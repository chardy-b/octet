# Source-independent DAC recovery ladder

The normal source controllers retain responsibility for their own casts/journals.
The DAC coordinator repairs output hardware; it never automatically resumes MPD. A cast is ended through its existing recovery, returning to the
saved speaker profile. A steady USB input retains its configuration. MPD queues
may be playing, paused, stopped or empty. Auto Tune ownership is never overridden.

## Transactions and privileges

The coordinator captures source identity, preserves queue and
position, and holds the current source's shared audio lease before changes. After
cast cleanup it holds its own lease. Its busy flag excludes receiver admission
and manual controls while the worker releases the HTTP control lock during root
operations. Recovery journals and failed states continue to block new playback commands.

The authenticated Unix socket accepts only fixed operation words: begin, stream,
driver, finish, reboot, plus legacy check/boot-ready. It accepts no paths, command
arguments, unit names or configuration bytes. The root broker verifies the DAC's
actual IO thread belongs to CamillaDSP, stalled hardware progress and the selected hardware output.
It records a separate root-owned receipt before every privileged step. Operations
cannot skip ordering or start a new ladder within the one-hour cooldown.

1. **Stream:** stop receiver/GUI services and restart CamillaDSP without a startup mute latch. If cast cleanup already restored DAC progress,
   finish without restarting anything.
2. **Driver:** only after the stream attempt failed, stop DSP and require every
   Loopback/DAC PCM endpoint closed. Unbind the one sound-card machine device,
   then its one DesignWare I2S device. Rebind in reverse order, including after a
   partial failure. Restart only the user's WirePlumber owner-free, verify all
   five nodes, then start DSP. Never unload ALSA modules, force-detach
   devices, or reset the shared RP1 DMA controller.
3. **Reboot:** only after both local options were attempted or refused. A durable
   initial stall receipt supplies evidence even if the failed driver step left
   the PCM absent. Refuse if DAC progress has returned. Retain the existing boot
   boot graph gate and no-master-mute policy and once-per-boot/hour reboot limit.

Finish requires complete graph, advancing DAC, exact configuration/path,
queue/position and listening-level verification. Held services are restored while
the coordinator still blocks receiver starts. A deliberate Music-page action is
required before casts can automatically start again. USB resumes on its selected input; there is no master-mute latch.

## Kernel-specific driver validation

The sysfs operation is not enabled merely because its paths exist. A root-owned
`/var/lib/octet-dac-recovery/driver-validation.json` must equal `driver_identity()`
(running kernel release plus loaded I2S module srcversion). Missing/stale records
refuse the rebind and leave reboot as the fallback. A kernel update invalidates
this permission. Never publish a validation record based solely on CI tests.

For an attended trial with casting stopped: preserve the full audio gate snapshot; stop casting;
restore the canonical saved profile; pause MPD; stop receivers,
GUI and remote; take the shared audio lease; restart DSP and verify exact
profile and DAC progress. Stop DSP and verify all PCM endpoints closed. Exercise
`rebind()` from the exact staged helper, verify graph before DSP start, then exact
profile/level/progress afterward. Publish the root validation record only if every
gate passes. Preserve the trial log and before/after PIDs. Restore services with MPD paused; receivers remain held until the transaction finishes.

The broker unit allows 90 seconds per step, and the client waits 95 seconds. No
root operation is retried automatically after an ambiguous response. Partial
driver reattach failures are reported and retained; a reboot can still be
requested from the recorded, exhausted driver stage if its safety gates pass.

Healthy-device trials establish reset/re-enumeration behavior, not proof of
recovery from an actual wedged DMA controller. Natural-stall evidence and an
attended physical output check remain necessary to establish that stronger claim.
