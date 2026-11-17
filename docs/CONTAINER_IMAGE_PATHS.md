# Container image path mapping for the publisher

This document covers the adapter-side path translation needed when the local
Xiaohongshu bridge runs in its approved Docker container and the host drafts
directory is mounted read-only at `/app/images`.

## Scope

The change is deliberately limited to outgoing image paths:

- `MockPublisher` is unchanged.
- Approval binding, source/account checks, daily limits, content hashes,
  publish receipts, `unknown` handling and the loopback/SSRF guard are unchanged.
- No service was started, no credentials were read, no account was switched, and
  no real API, message, approval or publish was exercised.

## Configuration

The two optional keys live under `[publisher.xiaohongshu]`:

```toml
[publisher.xiaohongshu]
image_host_root = "var/drafts"
image_container_root = "/app/images"
```

Rules:

- **Both absent:** the native Windows bridge behaviour is preserved. The
  existing absolute host image path is sent unchanged.
- **Both present:** the mapped behaviour is enabled.
- **Only one present:** the publisher refuses to construct.
- `image_host_root` is resolved relative to `config.root` (the project root).
  It must resolve inside that project root, and if it already exists it must be
  a directory.
- `image_container_root` must be a normalized, absolute POSIX path. Relative
  paths, URLs, backslashes, drive prefixes (`C:`), UNC-style paths, `//` roots,
  `.`/`..` segments, and non-normalized forms such as a trailing slash are
  rejected.

`config/bot.example.toml` shows both keys commented out. The prepared
`config/bot.live.toml` sets exactly:

```toml
image_host_root = "var/drafts"
image_container_root = "/app/images"
```

This only enables translation. It does not enable publishing, change the
account, or alter `live_enabled`.

## Per-image validation

When mapping is enabled, every image is checked before the publish POST:

1. Resolve the configured host root once at publisher construction, following
   existing symlinks/junctions, and require it to be inside the project root.
2. Resolve each image path with the filesystem's real path, requiring the file
   to exist.
3. Reject the image if the resolved path is outside the resolved host root.
   Component-aware containment also rejects prefix collisions such as
   `var/drafts-evil/...`.
4. Reject a directory (or any non-regular file).
5. Translate only the path relative to the host root:

   ```text
   <project>\var\drafts\campaign\card.png
       -> /app/images/campaign/card.png
   ```

   If the same image is not under the host root, no relative path is produced
   and no POST is attempted.
6. Keep the rest of the publish request unchanged: title, body, tags, draft id
   and version, approval code, scheduled time, body hash and per-image hashes are
   still sent exactly as before.

The local approval/content checks still run in `bot/services/publish.py` before
the adapter is called. Those checks continue to hash the approved host files;
this mapping only changes the path strings sent to the container bridge.

## Limitations

- This does not prove the container is running, that `/app/images` is mounted,
  that the account is logged in, or that Docker publishing works. Those are
  intentionally unverified here.
- Validation is lexical after filesystem resolution. A local process that can
  replace a file or symlink after resolution but before the bridge reads it
  (a time-of-check/time-of-use race) is outside this adapter's control.
- Symlinks and junctions are followed and their targets are containment-checked.
  Hard links cannot be distinguished from ordinary files by path resolution and
  are treated as being inside the root.
- The configured container root is not checked against the actual container
  mount. It must match the launcher's mount (`../var/drafts` to `/app/images`).
- `image_mode = "base64"` still gets the same host-root validation when mapping
  is configured, but its outgoing value is a data URL rather than a container
  path.

## Offline tests

`tests/test_container_image_paths.py` uses a fake loopback HTTP client only. It
covers:

- native absolute path when mapping is absent;
- a valid nested image mapped to `/app/images/...`;
- outside-root, prefix-collision, `..` escape, missing file and directory
  rejection before any POST;
- symlink escape, with a Windows directory-junction fallback when symlink
  creation is unavailable;
- half-configured and malformed mapping rejection;
- unchanged request metadata and image hashes.

Run it with the project environment:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_container_image_paths tests.test_approval_publish tests.test_xhs_identity -v
```

The tests do not contact a bridge, platform, Docker daemon, or real API.
