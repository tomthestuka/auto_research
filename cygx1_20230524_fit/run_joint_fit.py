import json
import traceback
from pathlib import Path

from research_toolkit.xspec_utils import (
    load_data,
    fit_tbabs_hss,
    fit_tbfeo_hss,
    run_fit,
    xspec_status,
    plot_fit,
    plot_iqu,
)

out = {"epoch": {"NuSTAR": ["80902318002"], "NICER": ["6643010101", "6643010102"], "IXPE": ["02008401"], "STATE": "soft"}}
figures = []

try:
    epoch = out["epoch"]
    indices = load_data(epoch)
    out["indices"] = indices
    out["status_after_load"] = xspec_status(indices)

    fit_tbabs_hss(indices)
    tbabs_runs = []
    for i in range(4):
        tbabs_runs.append(run_fit())
    out["tbabs_fit_runs"] = tbabs_runs
    out["tbabs_status"] = xspec_status(indices)
    try:
        fig = plot_fit(indices, outfile="cygx1_20230524_tbabs_broadband_fit.png")
        out["tbabs_plot_fit_return"] = fig
        figures.append(fig)
    except TypeError:
        fig = plot_fit(indices)
        out["tbabs_plot_fit_return"] = fig
        figures.append(fig)
    try:
        iqu = plot_iqu(indices, outprefix="cygx1_20230524_tbabs_iqu")
        out["tbabs_plot_iqu_return"] = iqu
        figures.extend(iqu if isinstance(iqu, list) else [iqu])
    except TypeError:
        iqu = plot_iqu(indices)
        out["tbabs_plot_iqu_return"] = iqu
        figures.extend(iqu if isinstance(iqu, list) else [iqu])

    try:
        fit_tbfeo_hss(indices)
        tbfeo_runs = []
        for i in range(4):
            tbfeo_runs.append(run_fit())
        out["tbfeo_fit_runs"] = tbfeo_runs
        out["tbfeo_status"] = xspec_status(indices)
        try:
            fig2 = plot_fit(indices, outfile="cygx1_20230524_tbfeo_broadband_fit.png")
            out["tbfeo_plot_fit_return"] = fig2
            figures.append(fig2)
        except TypeError:
            fig2 = plot_fit(indices)
            out["tbfeo_plot_fit_return"] = fig2
            figures.append(fig2)
    except Exception as e:
        out["tbfeo_error"] = repr(e)
        out["tbfeo_traceback"] = traceback.format_exc()

    out["figures"] = figures
    out["png_files"] = sorted(str(p) for p in Path('.').glob('*.png'))

except Exception as e:
    out["error"] = repr(e)
    out["traceback"] = traceback.format_exc()

Path("joint_fit_results.json").write_text(json.dumps(out, indent=2, sort_keys=True))
print(json.dumps(out, indent=2, sort_keys=True))
