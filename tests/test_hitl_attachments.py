"""E11: files on the human channel — receipt resolution, reply minting, and Telegram media."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from auto_research.hitl import telegram as tg
from auto_research.hitl.attachments import mint_reply_attachments, resolve_attachments
from auto_research.hitl.telegram import TelegramHandler
from auto_research.provenance.store import ReceiptStore, ScopedLookup
from auto_research.schemas import HumanAttachment, HumanQuery, HumanResponse


def _png(path: Path, size=(40, 30)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "white").save(path)
    return path


# ── resolve_attachments / mint_reply_attachments ─────────────────────────────


def test_resolve_attaches_what_the_sender_may_cite_and_names_the_rest(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    fig = store.mint(source="analyst_worker_0", kind="figure", produced_by="t",
                     path=_png(tmp_path / "w0" / "pd.png"), caption="PD vs energy", mime_type="image/png")
    data_file = tmp_path / "w0" / "bands.csv"
    data_file.write_text("band,pd\n2-3,3.5\n")
    data = store.mint(source="analyst_worker_0", kind="data", produced_by="t", path=data_file)
    text = store.mint(source="analyst_worker_0", kind="result", produced_by="t", data="fit ok")
    peer = store.mint(source="analyst_worker_1", kind="figure", produced_by="t",
                      path=_png(tmp_path / "w1" / "x.png"), caption="peer", mime_type="image/png")
    scope = ScopedLookup(store, own_source="analyst_worker_0")

    atts, problems = resolve_attachments(
        scope, [fig.handle, data.handle, text.handle, peer.handle, fig.handle, " "]
    )

    assert [a.handle for a in atts] == [fig.handle, data.handle, text.handle]  # deduped, ordered
    assert atts[0].filename == "pd.png" and atts[0].mime_type == "image/png"
    assert atts[0].caption == "PD vs energy"
    assert atts[1].mime_type == "text/csv"
    assert atts[2].filename == "analyst_worker_0_r1.txt" and atts[2].mime_type == "text/plain"
    assert problems == [f"{peer.handle} is not a receipt you can cite, so it was not attached."]


def test_bytes_changed_after_their_receipt_are_still_sent_with_an_alert(tmp_path):
    """F26, human ruling 2026-10-06: a changed file is sent as it is now, and alerted."""
    from auto_research.provenance import store as S

    alerts = []
    token = S._DRIFT_SINK.set(lambda summary, data: alerts.append(summary))
    try:
        store = ReceiptStore(tmp_path / "receipts")
        path = _png(tmp_path / "pd.png")
        fig = store.mint(source="s", kind="figure", produced_by="t", path=path, caption="c",
                         mime_type="image/png")
        _png(path, size=(41, 30))  # rewritten after the receipt
        (att,), problems = resolve_attachments(ScopedLookup(store, own_source="s"), [fig.handle])
    finally:
        S._DRIFT_SINK.reset(token)
    assert problems == [] and att.path.read_bytes() == path.read_bytes()
    assert store.root in att.path.parents  # a copy out of the agent's reach, not the live file
    assert alerts and "differs from its receipt" in alerts[0]


def test_a_receipted_file_swapped_for_a_symlink_is_not_attached(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    path = _png(tmp_path / "pd.png")
    fig = store.mint(source="s", kind="figure", produced_by="t", path=path, caption="c",
                     mime_type="image/png")
    secret = tmp_path / "secret"
    secret.write_bytes(b"token")
    path.unlink()
    path.symlink_to(secret)
    atts, problems = resolve_attachments(ScopedLookup(store, own_source="s"), [fig.handle])
    assert atts == [] and "symlink" in problems[0]


def test_resolve_sends_a_papers_original_pdf(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    paper = store.mint(source="lib", kind="paper", produced_by="open_paper", data="extracted text",
                       source_path=pdf, source_sha256=__import__("hashlib").sha256(pdf.read_bytes()).hexdigest(),
                       title="Krawczynski+2022")
    (att,), problems = resolve_attachments(ScopedLookup(store, own_source="lib"), [paper.handle])
    assert problems == [] and att.path.read_bytes() == pdf.read_bytes()
    assert att.filename == "paper.pdf" and att.mime_type == "application/pdf"
    assert att.caption == "Krawczynski+2022"


def test_mint_reply_attachments_makes_figures_and_data_under_the_reply(tmp_path):
    store = ReceiptStore(tmp_path / "receipts")
    reply = store.mint(source="analyst_lead", kind="human", produced_by="message", data="see attached")
    csv = tmp_path / "in" / "t.csv"
    csv.parent.mkdir()
    csv.write_text("a,b\n1,2\n")
    response = HumanResponse(query_id="hq-1", answer="see attached", attachments=[
        HumanAttachment(path=_png(tmp_path / "in" / "p.jpg"), filename="p.jpg", mime_type="image/jpeg"),
        HumanAttachment(path=csv, filename="t.csv", mime_type="text/csv", caption="my table"),
        HumanAttachment(path=tmp_path / "in" / "gone.bin", filename="gone.bin", mime_type="application/octet-stream"),
    ])

    fig, data = mint_reply_attachments(store, source="analyst_lead", response=response, parent=reply.handle)

    assert fig.kind == "figure" and fig.handle == "analyst_lead:f1" and fig.parents == [reply.handle]
    assert fig.caption == "from the human: p.jpg"
    assert data.kind == "data" and data.caption == "my table" and data.parents == [reply.handle]
    assert [a.handle for a in response.attachments] == [fig.handle, data.handle, None]


# ── Telegram fakes ───────────────────────────────────────────────────────────


class _Bot:
    def __init__(self, refuse_rich=False, refuse_photo=False, files=None):
        self._id = 100
        self.calls: list[tuple[str, dict]] = []
        self.refuse_rich = refuse_rich
        self.refuse_photo = refuse_photo
        self.files = files or {}

    def _post(self, method, kwargs):
        self._id += 1
        self.calls.append((method, kwargs))
        return SimpleNamespace(message_id=self._id)

    async def do_api_request(self, endpoint, api_kwargs=None, return_type=None, **_):
        from telegram.error import BadRequest

        if self.refuse_rich:
            raise BadRequest("can't parse rich message")
        return self._post(endpoint, api_kwargs)

    async def send_message(self, *, chat_id, text, parse_mode=None, reply_markup=None, **kw):
        return self._post("send_message", {"text": text, **kw})

    async def send_photo(self, **kwargs):
        from telegram.error import BadRequest

        if self.refuse_photo:
            raise BadRequest("PHOTO_INVALID_DIMENSIONS")
        return self._post("send_photo", kwargs)

    async def send_document(self, **kwargs):
        return self._post("send_document", kwargs)

    async def get_file(self, file_id):
        data = self.files[file_id]

        async def download_to_drive(dest):
            Path(dest).write_bytes(data)

        return SimpleNamespace(download_to_drive=download_to_drive)

    async def set_my_commands(self, commands):
        pass


async def _noop(*a, **k):
    return None


def _handler(bot) -> TelegramHandler:
    app = SimpleNamespace(
        bot=bot, add_handler=lambda h: None, initialize=_noop, start=_noop, stop=_noop,
        shutdown=_noop, updater=SimpleNamespace(start_polling=_noop, stop=_noop),
    )
    return TelegramHandler(app=app, chat_id="555", timeout_s=5)


def _msg(mid, *, text=None, caption=None, photo=None, document=None, reply_to=None, group=None):
    return SimpleNamespace(
        message_id=mid, text=text, caption=caption, photo=photo, document=document,
        media_group_id=group, chat=SimpleNamespace(id="555"), from_user=SimpleNamespace(is_bot=False),
        reply_to_message=SimpleNamespace(message_id=reply_to) if reply_to else None,
    )


def _update(msg):
    return SimpleNamespace(effective_message=msg, effective_chat=msg.chat, message=msg)


async def _open(handler, query):
    task = asyncio.create_task(handler(query))
    for _ in range(500):
        if handler._pending:
            break
        await asyncio.sleep(0)
    return task


def _query(tmp_path, attachments=(), reply_dir=None):
    return HumanQuery(message="Which band?", asked_by="analyst_lead", kind="clarify_science",
                      attachments=list(attachments), reply_dir=reply_dir)


def _fig_att(tmp_path, name="pd.png", size=(40, 30)):
    return HumanAttachment(handle="analyst_worker_0:f1", path=_png(tmp_path / name, size),
                           filename=name, mime_type="image/png", caption="PD vs energy")


# ── Telegram outbound ────────────────────────────────────────────────────────


def test_telegram_embeds_attachments_in_the_rich_message(tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    doc = HumanAttachment(handle="lib:p1", path=pdf, filename="paper.pdf", mime_type="application/pdf")
    handler = _handler(_Bot())

    async def run():
        task = await _open(handler, _query(tmp_path, [_fig_att(tmp_path), doc]))
        handler.on_message(_update(_msg(900, text="the first", reply_to=next(iter(handler._pending)))))
        return await task

    assert asyncio.run(run()).answer == "the first"
    ((method, kwargs),) = handler.app.bot.calls
    assert method == "sendRichMessage"
    rich = kwargs["rich_message"]
    assert '![](tg://photo?id=a1 "analyst_worker_0:f1 — PD vs energy")' in rich["markdown"]
    assert "![](tg://document?id=a2" in rich["markdown"]
    assert rich["media"] == [
        {"id": "a1", "media": {"type": "photo", "media": "attach://a1"}},
        {"id": "a2", "media": {"type": "document", "media": "attach://a2"}},
    ]
    assert kwargs["a2"].filename == "paper.pdf"


def test_embedded_files_upload_as_the_multipart_fields_the_media_names(tmp_path):
    """The request python-telegram-bot builds: rich_message in JSON, each file its own part."""
    from telegram.request import RequestData
    from telegram.request._requestparameter import RequestParameter

    handler = _handler(_Bot())
    asyncio.run(handler._send_rich("hello", attachments=(_fig_att(tmp_path),)))
    ((_, kwargs),) = handler.app.bot.calls
    data = RequestData([RequestParameter.from_input(k, v) for k, v in kwargs.items()])
    params = data.json_parameters
    assert "a1" not in params  # the file is not a JSON parameter
    assert json.loads(params["rich_message"])["media"][0]["media"]["media"] == "attach://a1"
    assert set(data.multipart_data) == {"a1"}


def test_refused_rich_message_sends_files_as_replies_that_answer_the_question(tmp_path):
    handler = _handler(_Bot(refuse_rich=True))

    async def run():
        task = await _open(handler, _query(tmp_path, [_fig_att(tmp_path)]))
        for _ in range(500):
            if len(handler._pending) == 2:
                break
            await asyncio.sleep(0)
        photo_id = max(handler._pending)
        handler.on_message(_update(_msg(900, text="replied to the photo", reply_to=photo_id)))
        return await task

    assert asyncio.run(run()).answer == "replied to the photo"
    (text_call, photo_call) = handler.app.bot.calls
    assert text_call[0] == "send_message" and photo_call[0] == "send_photo"
    assert photo_call[1]["reply_parameters"].message_id == 101
    assert photo_call[1]["caption"] == "analyst_worker_0:f1 — PD vs energy"


def test_a_photo_telegram_rejects_is_resent_as_a_document(tmp_path):
    handler = _handler(_Bot(refuse_photo=True))
    asyncio.run(handler._send_files((_fig_att(tmp_path),), SimpleNamespace(message_id=5)))
    assert [m for m, _ in handler.app.bot.calls] == ["send_document"]


def test_is_photo_follows_the_send_photo_limits(tmp_path):
    assert tg._is_photo(_fig_att(tmp_path))
    assert not tg._is_photo(_fig_att(tmp_path, "tall.png", size=(2100, 100)))  # ratio > 20
    assert not tg._is_photo(_fig_att(tmp_path, "big.png", size=(6000, 4500)))  # sides > 10000
    pdf = HumanAttachment(path=tmp_path / "x.pdf", filename="x.pdf", mime_type="application/pdf")
    assert not tg._is_photo(pdf)


def test_an_attachment_over_the_document_limit_is_noted_not_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "_DOCUMENT_MAX_BYTES", 10)
    handler = _handler(_Bot())

    async def run():
        task = await _open(handler, _query(tmp_path, [_fig_att(tmp_path)]))
        handler.on_message(_update(_msg(900, text="ok", reply_to=next(iter(handler._pending)))))
        return await task

    response = asyncio.run(run())
    assert response.notes and "exceeds Telegram's 50 MB limit" in response.notes[0]
    ((_, kwargs),) = handler.app.bot.calls
    assert "media" not in kwargs["rich_message"] and "exceeds" in kwargs["rich_message"]["markdown"]


# ── Telegram inbound ─────────────────────────────────────────────────────────


def _photo(file_id, size=1000):
    return [SimpleNamespace(file_id=file_id + "_small", file_size=10),
            SimpleNamespace(file_id=file_id, file_size=size)]


def test_a_photo_reply_is_saved_and_its_caption_is_the_answer(tmp_path):
    handler = _handler(_Bot(files={"big": b"jpegbytes"}))

    async def run():
        task = await _open(handler, _query(tmp_path, reply_dir=tmp_path / "replies"))
        mid = next(iter(handler._pending))
        await handler.handle_media(_update(_msg(900, caption="use this band", photo=_photo("big"), reply_to=mid)))
        return await task

    response = asyncio.run(run())
    assert response.answer == "use this band" and response.notes == []
    (att,) = response.attachments
    assert att.path == tmp_path / "replies" / "900_photo_900.jpg" and att.path.read_bytes() == b"jpegbytes"
    assert att.mime_type == "image/jpeg" and att.handle is None


def test_an_album_answers_once_with_every_item(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "_ALBUM_SETTLE_S", 0.01)
    doc = SimpleNamespace(file_id="d1", file_size=5, file_name="../../etc/table.csv", mime_type="text/csv")
    handler = _handler(_Bot(files={"p1": b"img", "d1": b"a,b"}))

    async def run():
        task = await _open(handler, _query(tmp_path, reply_dir=tmp_path / "replies"))
        mid = next(iter(handler._pending))
        await handler.handle_media(_update(_msg(901, caption="two files", photo=_photo("p1"), reply_to=mid, group="g")))
        await handler.handle_media(_update(_msg(902, document=doc, group="g")))
        return await task

    response = asyncio.run(run())
    assert response.answer == "two files"
    assert [a.path.name for a in response.attachments] == ["901_photo_901.jpg", "902_table.csv"]
    assert all(a.path.parent == tmp_path / "replies" for a in response.attachments)


def test_files_are_noted_not_saved_without_a_reply_dir_or_over_the_download_limit(tmp_path):
    handler = _handler(_Bot(files={"p": b"x"}))

    async def run(reply_dir, size):
        task = await _open(handler, _query(tmp_path, reply_dir=reply_dir))
        mid = next(iter(handler._pending))
        await handler.handle_media(_update(_msg(903, photo=_photo("p", size), reply_to=mid)))
        return await task

    no_dir = asyncio.run(run(None, 10))
    assert no_dir.attachments == [] and "nowhere to store files" in no_dir.notes[0]
    too_big = asyncio.run(run(tmp_path / "r", 21 * 1024 * 1024))
    assert too_big.attachments == [] and "at most 20 MB" in too_big.notes[0]


def test_query_hint_offers_files_only_when_the_reply_can_carry_them(tmp_path):
    assert "photo, file or album" in tg._query_markdown(_query(tmp_path, reply_dir=tmp_path))
    assert "photo, file or album" not in tg._query_markdown(_query(tmp_path))


@pytest.mark.parametrize("name,expected", [
    ("../../etc/passwd", "passwd"), ("my plot (1).png", "my_plot_1_.png"), ("...", "file"),
])
def test_safe_filename(name, expected):
    assert tg._safe_filename(name) == expected


# ── notify (seat-to-seat mirror) and the ambiguous-reply notice ──────────────


def test_notify_posts_silently_and_opens_no_question():
    handler = _handler(_Bot())
    asyncio.run(handler.notify("💬 analyst_worker_0 → analyst_lead\n\nfit converged"))
    ((method, kwargs),) = handler.app.bot.calls
    assert method == "sendRichMessage" and kwargs["disable_notification"] is True
    assert handler._pending == {}


def test_notify_falls_back_to_a_silent_html_message():
    handler = _handler(_Bot(refuse_rich=True))
    asyncio.run(handler.notify("hello"))
    ((method, kwargs),) = handler.app.bot.calls
    assert method == "send_message" and kwargs["disable_notification"] is True


def _two_open(handler, tmp_path):
    async def run(drive):
        a = asyncio.create_task(handler(_query(tmp_path, reply_dir=tmp_path / "r")))
        b = asyncio.create_task(handler(_query(tmp_path, reply_dir=tmp_path / "r")))
        for _ in range(500):
            if len(handler._pending) == 2:
                break
            await asyncio.sleep(0)
        await drive()
        for _ in range(50):
            await asyncio.sleep(0)
        first, second = sorted(handler._pending)
        handler.on_message(_update(_msg(990, text="a", reply_to=first)))
        handler.on_message(_update(_msg(991, text="b", reply_to=second)))
        return await a, await b
    return run


def test_a_plain_reply_with_two_questions_open_gets_a_notice_not_an_answer(tmp_path):
    handler = _handler(_Bot())

    async def drive():
        handler.on_message(_update(_msg(900, text="which one?")))

    first, second = asyncio.run(_two_open(handler, tmp_path)(drive))
    assert (first.answer, second.answer) == ("a", "b")  # the plain message answered neither
    notices = [k["text"] for m, k in handler.app.bot.calls if m == "send_message"]
    assert notices == [notices[0]] and "2 questions are open" in notices[0]


def test_an_unmatched_album_gets_one_notice(tmp_path):
    handler = _handler(_Bot(files={"p": b"x"}))

    async def drive():
        await handler.handle_media(_update(_msg(901, photo=_photo("p"), group="g")))
        await handler.handle_media(_update(_msg(902, photo=_photo("p"), group="g")))

    first, _ = asyncio.run(_two_open(handler, tmp_path)(drive))
    assert first.attachments == []
    assert sum(m == "send_message" for m, _ in handler.app.bot.calls) == 1


def test_an_attachment_over_the_chat_ceiling_is_refused_before_it_is_read(tmp_path, monkeypatch):
    from auto_research.hitl import attachments as A

    store = ReceiptStore(tmp_path / "receipts")
    path = _png(tmp_path / "big.png")
    fig = store.mint(source="s", kind="figure", produced_by="t", path=path, caption="c",
                     mime_type="image/png")
    monkeypatch.setattr(A, "MAX_ATTACHMENT_BYTES", 10)
    monkeypatch.setattr(A, "read_receipted", lambda *a, **k: pytest.fail("read before the size check"))
    atts, problems = resolve_attachments(ScopedLookup(store, own_source="s"), [fig.handle])
    assert atts == [] and "MB" in problems[0]
