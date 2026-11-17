from brain import alert

TB = '''Traceback (most recent call last):
  File "/home/runner/work/HD/HD/brain/run.py", line 140, in main
  File "/home/runner/work/HD/HD/brain/community.py", line 211, in collect
  File "/usr/lib/python3.12/site-packages/requests/api.py", line 73, in get
requests.exceptions.ConnectionError: Max retries exceeded
'''


def test_python_error(tmp_path):
    (tmp_path / "brain_run.log").write_text(TB)
    msg = alert.build({"OUT_BRAIN": "failure", "RUN_URL": "u"}, tmp_path)
    assert "Juntar datos" in msg and "brain/community.py:211" in msg
    assert "ConnectionError" in msg and "no respondió" in msg


def test_git_error(tmp_path):
    (tmp_path / "save.log").write_text("error: cannot pull with rebase: You have unstaged changes.\n"
                                       "::error::could not push the daily update after 3 tries\n")
    msg = alert.build({"OUT_SAVE": "failure"}, tmp_path)
    assert "Guardar en GitHub" in msg and "unstaged changes" in msg and "sin guardar" in msg


def test_simulated(tmp_path):
    msg = alert.build({"OUT_SIMULATED": "failure"}, tmp_path)
    assert "Fallo simulado" in msg and "a propósito" in msg
