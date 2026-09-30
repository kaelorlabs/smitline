# Vendored joinly subset

Smitline joins Microsoft Teams and Google Meet in a browser with a few modules from
[joinly](https://github.com/joinly-ai/joinly) (MIT; see `LICENSE`): the Playwright browser
session, the PulseAudio and Xvfb virtual devices, the camera feed, and the Teams and Meet
controllers. GPT-Live does all listening and speaking, so joinly's speech models, services,
server, and client were removed. The package is put on `PYTHONPATH` in `Dockerfile.meeting`.
