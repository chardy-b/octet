"""The command line module is not exercised by the DSP tests, so make sure it at least imports and parses."""
import pytest

from octetmeasure import cli


def test_cli_imports_and_parses_every_command(capsys):
    for argv in (["list"], ["devices"], ["peq", "x"], ["export", "x"], ["analyze", "x"], ["sweep-file", "--channel", "left", "--invert", "right"],
                 ["record", "x", "--play-dlna", "wiim", "--channel", "both", "--invert", "right"]):
        # parse only: every sub-command must accept its arguments; running them needs hardware or data
        with pytest.raises(SystemExit) as e:
            cli.main(argv + ["--help"])
        assert e.value.code == 0
    capsys.readouterr()
