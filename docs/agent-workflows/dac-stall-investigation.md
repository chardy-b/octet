# DAC stall investigation, 2026-11-12

## Evidence

Octet ran `6.18.50+rpt-rpi-2712` (Debian `1:6.18.50-1+rpt1`, built
2026-09-11), CamillaDSP 4.1.3 (`05e9cfc`), DesignWare I2S srcversion
`444B36F9CE4656F39DC1C07`. DMA debugfs maps `dma2chan2` to
`1f000a0000.i2s:tx`. All five desktop Loopback/DAC nodes remained present.

At 02:42:19, Camilla logged a capture overrun (`snd_pcm_readi`, EPIPE),
then capture and playback stalled. The kernel logged TX DMA failed-to-stop /
non-idle errors. At 12:44:57 the Tidal-to-AirPlay route change reopened playback,
but DAC `hw_ptr` remained 3, with repeated DMA errors every two seconds.
AirPlay capture used 44100 Hz stereo S32_LE; DAC playback used 48000 Hz,
eight channels, S32_LE, 512-frame periods and a 4096-frame hardware buffer.
The route had asynchronous resampling and rate adjustment enabled. The
watchdog confirmed an AirPlay DAC stall at 12:45:05. Tidal remained paused.

This supports a stop/restart driver failure. It does not establish whether
clock drift, scheduling, a source transition or an upstream interruption first
caused the overrun. The first error preceded the AirPlay switch by ten hours.
Do not describe the source switch as the proven initial cause.

## Upstream prevention candidate

- [Raspberry Pi issue 7584](https://github.com/raspberrypi/linux/issues/7584)
  reports DAC8x multi-lane failures around underrun recovery.
- [Merged PR 7588](https://github.com/raspberrypi/linux/pull/7588) restores
  upstream `i2s_stop` teardown, including disabling DMA and transmitter state.
  It merged on 2026-10-02, after Octet's installed kernel build.
- [Issue 7322](https://github.com/raspberrypi/linux/issues/7322) describes a
  related Pi 5 failure to restart DMA after STOP on a different I2S device.

The configured stable Raspberry Pi archive offered `1:6.18.60-1+rpt1`.
The downloaded 2712 image package SHA256 was
`663099a4badd7e583ae28bbb0ef60fc3b6d1faafabbb8c6252a989730b9224e9`.
Disassembly of its actual DesignWare module shows the STOP path clearing ITER,
calling `i2s_disable_dma`, and clearing CER/IER when inactive. A version number
alone is not sufficient evidence that a future kernel includes this fix.

Use the stable package manager, preserve the previous boot files/kernel and
configuration, and follow the audio deployment gates. Never hot-swap the active
I2S module or induce underruns on connected speakers. After boot, verify the
loaded kernel/module identity, all graph nodes, saved profile, mute, queue and
advancing DAC pointers. Physical output mapping needs attended acceptance.

## Application changes

AirPlay's initial automatic unmute now additionally requires a stable DAC owner
and an advancing RUNNING DAC hardware pointer. Capture readiness and DSP
`Running` alone admitted the wedged output on this incident. This guard does not
prove physical channel mapping or repair a failed driver.

`audio_diagnostics.py` records 180 watchdog observations in memory (nominally
six minutes), including idle periods, route/clock settings, DAC and every
Loopback substream's counters, and capture-rate / rate-adjust / playback-buffer /
processing-load metrics sampled every ten seconds. A confirmed watchdog fault
saves evidence before the recovery callback to
`/var/lib/octet-remote/audio-incidents/incident-*.json`.

Incidents include current ALSA status and hardware/software parameters, kernel
and boot identity, and a bounded tail of Camilla's actual log file with WebSocket
disconnect noise removed. Journal reads record permission failures explicitly;
the current upmpdcli service user cannot read the system journal. Root kernel
logs must therefore also be preserved during operator investigation. No new
journal access privileges are granted.

Files use mode 0600, atomic rename and fsync. Retention is eight incidents, each
limited to one MiB. Healthy sampling does not write to disk. No audio, credentials,
track URLs or full filter profiles are recorded. Evidence failures cannot cancel
the existing recovery callback. Automatic reboot remains Tidal-only; cast or
tuning recovery still requires an operator.

## Follow-up acceptance

With the patched kernel and recorder active, observe ordinary playback before
changing resampler or buffer policy. Exercise Tidal/AirPlay/Bluetooth source
round trips only in an attended muted/low-level session. Verify each physical
output and its expected frequency band after recovery. Compare incidents for
the first PCM discontinuity, changing ownership/trigger time, rate-adjust trend,
buffer occupancy and the earliest kernel error. Change one variable at a time.
