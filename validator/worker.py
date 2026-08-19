from __future__ import annotations

import json
import sys


def main() -> int:
    import torch
    import torchaudio
    from qwen_asr import Qwen3ASRModel
    from speechbrain.inference.speaker import SpeakerRecognition

    payload = json.loads(sys.stdin.readline())
    device = payload["device"]
    model = Qwen3ASRModel.from_pretrained(
        payload["asr_model"],
        forced_aligner=payload["aligner_model"],
        dtype=torch.bfloat16,
        device_map=device,
        forced_aligner_kwargs={"dtype": torch.bfloat16, "device_map": device},
    )
    speaker = None
    if any(item.get("reference") for item in payload["items"]):
        speaker = SpeakerRecognition.from_hparams(
            source=payload["speaker_model"], run_opts={"device": "cpu"}
        )

    def waveform(path: str):
        audio, rate = torchaudio.load(path)
        audio = audio.mean(dim=0)
        if rate != 16_000:
            audio = torchaudio.functional.resample(audio, rate, 16_000)
        return audio

    def embedding(audio):
        assert speaker is not None
        value = speaker.encode_batch(audio.unsqueeze(0), normalize=False).flatten()
        return torch.nn.functional.normalize(value, dim=0)

    reference_embeddings = {}
    responses = []
    for item in payload["items"]:
        result = model.transcribe(
            audio=item["audio"],
            language=item["language"],
            return_time_stamps=True,
        )[0]
        alignment = []
        if result.time_stamps is not None:
            alignment = [
                {"text": token.text, "start": token.start_time, "end": token.end_time}
                for token in result.time_stamps
            ]
        scores = []
        reference = item.get("reference")
        if speaker is not None and reference:
            if reference not in reference_embeddings:
                reference_embeddings[reference] = embedding(waveform(reference))
            audio = waveform(item["audio"])
            window = 32_000
            hop = 16_000
            chunks = [audio[start : start + window] for start in range(0, len(audio), hop)]
            chunks = [chunk for chunk in chunks if len(chunk) >= 8_000] or [audio]
            scores = [
                float(torch.dot(reference_embeddings[reference], embedding(chunk)))
                for chunk in chunks
            ]
        responses.append(
            {
                "validator": "Qwen/Qwen3-ASR-0.6B + Qwen/Qwen3-ForcedAligner-0.6B",
                "transcript": result.text,
                "detected_language": result.language,
                "alignment": alignment,
                "identity_validator": "speechbrain-ecapa-voxceleb-window-v1",
                "identity_model_sha256": payload["speaker_model_sha256"],
                "identity_scores": scores,
            }
        )
    print("QVL_ASR " + json.dumps({"results": responses}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
