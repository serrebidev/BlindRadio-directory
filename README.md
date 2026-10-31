# BlindRadio Directory

Community-submitted stations for the BlindRadio Directory inside
[BlindRadio](https://github.com/serrebidev/BlindRadio). In the app, open
Browse and choose BlindRadio Directory: each playlist file here is a
folder, and every station in it can be played or added to your own
collection. The app reads this repo live, so merged changes show up in
Browse without an app update.

## Layout

One `.m3u` file per country or category (`Germany.m3u`,
`Classical Music.m3u`). Inside a file, group stations with the
`group-title` attribute (a network or provider, for example `BBC`):

```
#EXTM3U
#EXTINF:-1 group-title="BBC",BBC Radio 1
http://example.com/bbc_radio_one.m3u8
```

## Submitting stations

Every submission is checked before it is merged: the file must be in a
playlist format BlindRadio supports and every station must parse and
point at a playable stream.

Open a pull request that adds or edits a playlist file:

- One station is an `#EXTINF` line (name, and a `group-title` if it
  belongs to a group) followed by the stream URL on the next line.
- Stream URLs must play directly (no web pages, no login).
- Put the station in the file for its country or category, or start a
  new file if none fits.
- Keep names short and exact; remove stations that no longer work.

Maintained by Brandon Bracey (serrebidev) and amirsol81.
