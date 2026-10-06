import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from .analysis import Result  # noqa: E402


def plot_result(res: Result, path, title: str = "", window_ms: float = 10.0) -> None:
    r = res.response
    fig, ax = plt.subplots(3, 1, figsize=(10, 10), gridspec_kw=dict(height_ratios=[3, 2, 2]))

    ax[0].semilogx(r.freq, r.mag_db_raw, color="0.8", lw=0.8, label="unsmoothed")
    ax[0].semilogx(r.freq, r.mag_db, lw=1.8, label="smoothed")
    ax[0].set(ylabel="dB (re full scale)", title=title or "Frequency response", xlim=(r.freq[0], r.freq[-1]))
    top = np.nanmax(r.mag_db)
    ax[0].set_ylim(top - 60, top + 5)
    ax[0].grid(True, which="both", alpha=0.3)
    ax[0].legend(loc="lower left")

    ax[1].semilogx(r.freq, ((r.phase_deg + 180) % 360) - 180, lw=1.2)
    ax[1].set(ylabel="phase (deg)", xlim=(r.freq[0], r.freq[-1]), ylim=(-180, 180))
    ax[1].grid(True, which="both", alpha=0.3)

    p = int(round(res.peak))
    half = int(window_ms * 1e-3 * res.fs)
    lo, hi = max(0, p - half // 5), min(len(res.ir), p + half)
    t = (np.arange(lo, hi) - res.peak) / res.fs * 1000
    ax[2].plot(t, res.ir[lo:hi], lw=0.8)
    ax[2].set(xlabel="ms from main impulse", ylabel="impulse response")
    ax[2].grid(True, alpha=0.3)

    notes = []
    if res.delay_ms is not None:
        notes.append(f"acoustic delay {res.delay_ms:.3f} ms ({res.delay_ms * 34.3:.1f} cm)")
    if res.snr_db is not None:
        notes.append(f"SNR {res.snr_db:.0f} dB")
    if res.clipped:
        notes.append("CLIPPED - lower the gain")
    if notes:
        ax[2].set_title(", ".join(notes), loc="right", fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def plot_peq(info: dict, filters, fs: float, path, title: str = "") -> None:
    from .peq import eq_db
    f = info["f"]
    fig, ax = plt.subplots(2, 1, figsize=(10, 8), gridspec_kw=dict(height_ratios=[3, 2]), sharex=True)
    ax[0].semilogx(f, info["measured"], lw=1.6, label="measured (smoothed)")
    ax[0].semilogx(f, info["target"], lw=1.6, ls="--", label="target")
    ax[0].semilogx(f, info["predicted"], lw=2.0, label="measured + EQ (predicted)")
    ax[0].set(ylabel="dB, level-matched at 300 Hz - 3 kHz", title=title or "PEQ fit")
    ax[0].grid(True, which="both", alpha=0.3)
    ax[0].legend(loc="lower left")
    curve = eq_db(f, fs, filters)
    ax[1].semilogx(f, curve, lw=1.8, color="C3")
    ax[1].axhline(0, color="k", lw=0.6)
    for p in filters:
        ax[1].plot([p.fc], [p.gain_db], "o", color="C3")
        ax[1].annotate(f"{p.fc:g}", (p.fc, p.gain_db), textcoords="offset points", xytext=(0, 7 if p.gain_db >= 0 else -14),
                       ha="center", fontsize=8)
    ax[1].set(xlabel="Hz", ylabel="EQ gain (dB)")
    ax[1].grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
