# SobaFM

A self-hosted Discord bot that streams AI-generated music into voice channels.

A server manager places SobaFM in a voice channel, where it waits for requests. A member describes the music they want, such as "rainy lo-fi with soft piano" or "upbeat synthwave for a night drive." SobaFM turns the request into musical direction with [Gemini](https://ai.google.dev/gemini-api/docs) and plays it continuously with [Lyria RealTime](https://ai.google.dev/gemini-api/docs/realtime-music-generation) until the program runs its course or someone asks for something else. Each operator runs their own instance with their own Discord application and Gemini API key.

## Status

SobaFM is in design and not yet ready to run. Planning and progress are tracked in the [SobaFM project](https://github.com/users/slackysoba/projects/2).

## License

[MIT](LICENSE)
