# Vendored third-party files

Everything the web UI needs at runtime is in this directory. Nothing is fetched from
a CDN or any other network location when the page runs: `harness-manager-daemon` serves these
files itself. Each file was fetched once from `cdn.jsdelivr.net` (the npm package
named below) and is pinned to the version listed. `tests/web/test_t14_static.py`
checks that every file here is listed, that every listed file exists with this
sha256, and that each library directory holds its licence.

To update a library: fetch the new file from the same package path, update the
version and the sha256 here, and run `make check`.

| File | Package and version | Upstream path | Licence | sha256 |
|---|---|---|---|---|
| `preact/preact.module.js` | `preact@10.29.8` | `dist/preact.module.js` | MIT (`preact/LICENSE`) | `c30e721ebfdc6e2ad4c18c14d2dfb82667829c8aec27de1207774e3fc16858a8` |
| `preact/hooks.module.js` | `preact@10.29.8` | `hooks/dist/hooks.module.js` (patched, see below) | MIT (`preact/LICENSE`) | `f3555e7a8dba84175c94f17148ba6670e4b3716888fe06b3330a485637296a38` |
| `preact/LICENSE` | `preact@10.29.8` | `LICENSE` | | `1fe6958409c8c257a70c587a18b6f7f412b179b456630790d30b2ec9a8e4b7d4` |
| `htm/htm.module.js` | `htm@3.1.1` | `dist/htm.module.js` | Apache-2.0 (`htm/LICENSE`) | `ab33dd3f38059b9be4d5f5350128eefb2356639c4e0bbe9d9e8b3ba75847e9e4` |
| `htm/LICENSE` | `htm@3.1.1` | `LICENSE` | | `740725f7252e750af735d0028cc534970772f513331e9f68150fede8fb3ce00f` |
| `xterm/xterm.module.js` | `@xterm/xterm@6.0.0` | `lib/xterm.mjs` (renamed) | MIT (`xterm/LICENSE`) | `b336ec65a086c056d4804b3d4c2347da5663d3f23c3f25be866467bd8857ad59` |
| `xterm/xterm.css` | `@xterm/xterm@6.0.0` | `css/xterm.css` | MIT (`xterm/LICENSE`) | `854a7c0fb70e8b1a083c16797ab827299fb18744f5ad34f227b48337e33293c6` |
| `xterm/LICENSE` | `@xterm/xterm@6.0.0` | `LICENSE` | | `b569f629d00f2626a8100df2a1798210535621e42164dfd426a6fe5aac7b0ccd` |
| `xterm/addon-fit.module.js` | `@xterm/addon-fit@0.11.0` | `lib/addon-fit.mjs` (renamed) | MIT (`xterm/LICENSE-addon-fit`) | `2d87e1bddc73be9111de8beee5370c3bb7aac9c94e18e6f245f02ca741ef1769` |
| `xterm/LICENSE-addon-fit` | `@xterm/addon-fit@0.11.0` | `LICENSE` | | `e256f01188af527e4d06d21d06fbf785ae9c50d4b328bf03cbe0ba7f0aa4228f` |
| `fonts/ibm-plex-sans-latin-400-normal.woff2` | `@fontsource/ibm-plex-sans@5.3.0` | `files/ibm-plex-sans-latin-400-normal.woff2` | SIL OFL 1.1 (`fonts/LICENSE-ibm-plex-sans`) | `3b646991d30055a93a4ecc499713d4347953a74a947ecab435ab72070cbdab0e` |
| `fonts/ibm-plex-sans-latin-500-normal.woff2` | `@fontsource/ibm-plex-sans@5.3.0` | `files/ibm-plex-sans-latin-500-normal.woff2` | SIL OFL 1.1 (`fonts/LICENSE-ibm-plex-sans`) | `0717336fb31fcdcde4b8deb3675bb4a0f7f6d484864afcd6751ac29975962203` |
| `fonts/ibm-plex-sans-latin-600-normal.woff2` | `@fontsource/ibm-plex-sans@5.3.0` | `files/ibm-plex-sans-latin-600-normal.woff2` | SIL OFL 1.1 (`fonts/LICENSE-ibm-plex-sans`) | `8960851d691c054ed38e259bdcf1a6190d157b4203ed5bb32c632a863fb8ec2f` |
| `fonts/LICENSE-ibm-plex-sans` | `@fontsource/ibm-plex-sans@5.3.0` | `LICENSE` | | `d0283623ef57e722fd0eb688a8041589670c608ab780cd3612d06ba6f153d3fd` |
| `fonts/ibm-plex-mono-latin-400-normal.woff2` | `@fontsource/ibm-plex-mono@5.3.0` | `files/ibm-plex-mono-latin-400-normal.woff2` | SIL OFL 1.1 (`fonts/LICENSE-ibm-plex-mono`) | `08949f728dc52d528e69b1667d15c89a5686a4ee9a296ff90983985f99c380f7` |
| `fonts/ibm-plex-mono-latin-500-normal.woff2` | `@fontsource/ibm-plex-mono@5.3.0` | `files/ibm-plex-mono-latin-500-normal.woff2` | SIL OFL 1.1 (`fonts/LICENSE-ibm-plex-mono`) | `01d285447409c8a588692162439a038b8cbd7871309ee20267b0d2d91c6e8e22` |
| `fonts/LICENSE-ibm-plex-mono` | `@fontsource/ibm-plex-mono@5.3.0` | `LICENSE` | | `23b0a9d0c6d3f140a0b77e483c5cfa6bba574325ef5cb189ed9f2fec4884533f` |
| `lucide/icons.js` | `lucide-static@1.47.0` | `icons/<name>.svg` (generated, see below) | ISC (`lucide/LICENSE`) | generated |
| `lucide/LICENSE` | `lucide-static@1.47.0` | `LICENSE` | | `b495047bd93a9b06913511076f504daba17d5bbeb3e0650f3bb53a4220329c57` |

## Local changes

- **`preact/hooks.module.js`:** the one import `from"preact"` is rewritten to
  `from"./preact.module.js"`. With it the page needs no import map, so it runs
  under `script-src 'self'` with no inline script. Nothing else in the file changed;
  the upstream sha256 is `a6ee626f2d01570592dd569a792e3f050154aa02890eead8c223fa3ed5aa3d5a`.
- **`.mjs` renamed to `.module.js`:** some hosts serve `.mjs` with a type a module
  script refuses. The contents are byte-identical to upstream.
- **`lucide/icons.js`:** built from the SVG files of the icons the UI uses. Each
  value is the inner markup of the upstream `<svg>` element, whitespace-joined. To add
  an icon, fetch `lucide-static@1.47.0/icons/<name>.svg` and add its inner markup
  under its name. The browser tests fail on any icon name that is missing.
- The `//# sourceMappingURL=` comments are left as they are. The maps are not
  vendored, so a browser's developer tools report them missing; the page is unaffected.
