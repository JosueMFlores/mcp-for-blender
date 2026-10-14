import httpx
import pytest

from blender_mcp import local_generation


def test_generates_once_with_settings_and_stage_progress():
    requests, stages = [], []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=b"glTFtestdata!", request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        raw = local_generation.generate({"base_url": "http://localhost:8081", "settings": {"texture": True}},
                                        prompt="chair", progress=lambda *args: stages.append(args), client=client)
    assert raw.startswith(b"glTF") and len(requests) == 1
    assert requests[0].url.path == "/generate"
    assert b'"text":"chair"' in requests[0].content and b'"texture":true' in requests[0].content
    assert [p[0] for p in stages] == [1, 2]


def test_failed_http_is_not_retried():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(503, request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            local_generation.generate({"base_url": "http://localhost"}, prompt="chair", client=client)
    assert len(requests) == 1


def test_invalid_and_oversized_models_are_rejected(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"not a GLB", request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="GLB"):
            local_generation.generate({"base_url": "http://localhost"}, prompt="chair", client=client)
        monkeypatch.setattr(local_generation, "MAX_MODEL_BYTES", 4)
        with pytest.raises(ValueError, match="limit"):
            local_generation.generate({"base_url": "http://localhost"}, prompt="chair", client=client)


def test_image_is_encoded_without_a_network_get(tmp_path):
    image = tmp_path / "ref.png"
    image.write_bytes(b"reference")
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=b"glTFtestdata!", request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        local_generation.generate({"base_url": "http://localhost"}, image=str(image), client=client)
    assert len(requests) == 1 and b'"image":"cmVmZXJlbmNl"' in requests[0].content


@pytest.mark.parametrize("kwargs", [{}, {"prompt": "chair", "image": "file"}])
def test_requires_exactly_one_input(kwargs):
    with pytest.raises(ValueError, match="exactly one"):
        local_generation.generate({"base_url": "http://localhost"}, **kwargs)
