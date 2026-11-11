# Hold for Me models

The voice activity models behind `hold_listener.py`, run on the CPU with onnxruntime. They are
copied here unchanged from their upstream repositories and keep their own licenses; the
license texts are next to them.

| File | What it is | sha256 |
| --- | --- | --- |
| `fireredvad_stream_vad_with_cache.onnx` | FireRedVAD's streaming voice activity model (DFSMN). Takes 80 log-mel bins of 16 kHz audio every 10 ms (`feat [1,T,80]`) and its caches (`caches_in [8,1,128,19]`); gives a speech probability per frame (`probs [1,T,1]`) and the next caches. The default detector. | `b3c97836130dc34fc32d56fab551e88cf9454511de2b3c250a5e6578dee74b93` |
| `fireredvad_aed.onnx` | FireRedVAD's audio event model, not streaming: speech, singing and music probabilities per frame (`feat [B,T,80]` to `probs [B,T,3]`). The music check. | `4ec27ccd86b16c38aec043215d5d2e528c07eb3b7bad8f14d636633166cff137` |
| `cmvn.ark` | FireRedVAD's global mean and variance statistics for the 80 bins (a Kaldi binary matrix, 2 x 81), used by both models above. | `c87f6f13edf0f0ec7535ddfc9cc3387d9268cb234b70182d566c5e2edf3ca473` |
| `silero_vad.onnx` | Silero VAD v6 (the v6.2 model). 8 kHz (256-sample windows after 32 samples of context) or 16 kHz (512 after 64), with a `[2,1,128]` state; gives a speech probability per window. The alternative detector. | `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3` |
| `LICENSE-FireRedVAD` | FireRedVAD's license, the Apache License 2.0 (code and weights). | `c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4` |
| `LICENSE-silero-vad` | Silero VAD's license, MIT. | `2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b` |

## Where they come from

- **FireRedVAD**, https://github.com/FireRedTeam/FireRedVAD at commit
  `c30ec49e8cc69642b0ee65362eba11b9d11c6e54` (2026-05-06): the three model files from
  `pretrained_models/onnx_models/`, and `LICENSE`. Copyright 2026 Xiaohongshu. Apache-2.0, as
  the repository and its Hugging Face model card (FireRedTeam/FireRedVAD) say.
- **Silero VAD**, https://github.com/snakers4/silero-vad at tag `v6.2.3` (commit
  `5cd7945676eb32225748052e2e6a0580e4686a08`): `src/silero_vad/data/silero_vad.onnx`, the same
  file as at tag `v6.2`, and `LICENSE`. Copyright (c) 2020-present Silero Team. MIT.

To check the files: `sha256sum *` here. To update one, take it from the same path at a newer
commit or tag, put its hash in the table, and run `python -m unittest test_hold_listener`.
