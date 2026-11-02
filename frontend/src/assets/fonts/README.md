# Bundled fonts

IBM Plex Sans (400, 500, 600) and IBM Plex Mono (400, 500), Latin subset, WOFF2. They are served from this
application's own origin by Vite; the page makes no request to a font CDN.

- **License:** SIL Open Font License 1.1, Copyright IBM Corp. The full text for each family is kept beside the
  files (`OFL-IBM-Plex-Sans.txt`, `OFL-IBM-Plex-Mono.txt`). The fonts are unmodified.
- **Source:** the files were taken unmodified from the npm packages `@fontsource/ibm-plex-sans@5.3.0` and
  `@fontsource/ibm-plex-mono@5.3.0` (`files/*-latin-*-normal.woff2`), which republish the IBM Plex release.
  The packages are NOT dependencies of this project; only these files are vendored.
- **Subset:** Latin (`U+0000-00FF` plus common punctuation, the euro sign and a few arrows). Characters outside it
  fall back to the system font stack declared in `src/index.css`.

SHA-256 of the vendored files:

```
3b646991d30055a93a4ecc499713d4347953a74a947ecab435ab72070cbdab0e  ibm-plex-sans-latin-400-normal.woff2
0717336fb31fcdcde4b8deb3675bb4a0f7f6d484864afcd6751ac29975962203  ibm-plex-sans-latin-500-normal.woff2
8960851d691c054ed38e259bdcf1a6190d157b4203ed5bb32c632a863fb8ec2f  ibm-plex-sans-latin-600-normal.woff2
08949f728dc52d528e69b1667d15c89a5686a4ee9a296ff90983985f99c380f7  ibm-plex-mono-latin-400-normal.woff2
01d285447409c8a588692162439a038b8cbd7871309ee20267b0d2d91c6e8e22  ibm-plex-mono-latin-500-normal.woff2
```

To change the set, replace the files from a newer upstream release, update the hashes above, and keep the license
files. `src/theme/typography.test.ts` fails if a file referenced from `index.css` is missing or if a font URL
points off-site.
