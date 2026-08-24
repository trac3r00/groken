
from groken import doctor


class Response:
    def raise_for_status(self):
        return None


def test_all_tiers_pass_and_never_leak_secrets(monkeypatch, capsys):
    secret = "access-secret-123"
    monkeypatch.setattr(doctor, "load_tokens", lambda: {"accessToken": secret, "expiresIn": 60})
    monkeypatch.setattr(doctor, "load_config", lambda: {"podId": "pod-1", "model_base_url": "https://model.test"})
    class Manager:
        def ensure_sandbox_metadata(self):
            return {"podId": "pod-1", "execDaemonUrl": "https://exec.test"}
        def command(self, name):
            assert name == "listAgents"
            return []
    monkeypatch.setattr(doctor, "GatewayManager", Manager)
    class Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, *a, **kw): return Response()
        def head(self, *a, **kw): return Response()
    monkeypatch.setattr(doctor.httpx, "Client", Client)
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": "{}"})())
    assert doctor.run_doctor() == 0
    output = capsys.readouterr().out
    assert secret not in output
    assert "https://" not in output


def test_missing_tokens_is_hard_failure(monkeypatch):
    monkeypatch.setattr(doctor, "load_tokens", lambda: None)
    monkeypatch.setattr(doctor, "load_config", dict)
    monkeypatch.setattr(doctor, "GatewayManager", lambda: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **kw: type("R", (), {"returncode": 1, "stdout": ""})())
    assert doctor.run_doctor() == 1


def test_controller_down_and_pod_change_are_soft(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "load_tokens", lambda: {"accessToken": "secret"})
    monkeypatch.setattr(doctor, "load_config", lambda: {"podId": "old"})
    class Manager:
        def ensure_sandbox_metadata(self): return {"podId": "new", "execDaemonUrl": "https://exec.test"}
        def command(self, name): return []
    monkeypatch.setattr(doctor, "GatewayManager", Manager)
    class Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url, **kw): raise OSError("down")
        def head(self, *a, **kw): raise OSError("down")
    monkeypatch.setattr(doctor.httpx, "Client", Client)
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **kw: type("R", (), {"returncode": 1, "stdout": ""})())
    assert doctor.run_doctor() == 0
    assert "ALARM" in capsys.readouterr().out


def test_mcp_probe_is_bounded(monkeypatch):
    monkeypatch.setattr(doctor, "load_tokens", lambda: {"accessToken": "secret"})
    monkeypatch.setattr(doctor, "load_config", dict)
    monkeypatch.setattr(doctor, "GatewayManager", lambda: (_ for _ in ()).throw(RuntimeError()))
    def hanging(*args, **kwargs):
        assert kwargs["timeout"] == 5
        raise TimeoutError()
    monkeypatch.setattr(doctor.subprocess, "run", hanging)
    assert doctor.run_doctor() == 1
