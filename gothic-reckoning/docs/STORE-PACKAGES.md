# Gothic Reckoning — Store Package Status

Date: 2026-08-05. Evidence-backed state of the two native packages. Companion to `docs/STORE-REQUIREMENTS.md` (platform policy checklist).

## iOS (Apple App Store)

- Xcode project: `ios/App/App.xcodeproj` — builds and archives.
- **Verified 2026-08-05:** `xcodebuild -workspace App.xcworkspace -scheme App -configuration Release -destination 'generic/platform=iOS' CODE_SIGNING_ALLOWED=NO archive` → **ARCHIVE SUCCEEDED** (Xcode 26.6, unsigned; signing key is a human gate).
- App ID: `com.rodgersintelligence.gothicreckoning`.
- App icon 512px baked via the same icon source already in the repo.
- Remaining store-side items (human/platform gates): signing certificate + provisioning profile, App Store Connect metadata, privacy labels, screenshots.

## Android (Google Play)

- Gradle project: `android/`, namespace `com.rodgersintelligence.gothicreckoning`
- `targetSdkVersion 36`, `compileSdkVersion 36` — meets Google Play's Aug 31, 2026 target API requirement (Android 16 / API 36).
- Web assets copied into `android/app/src/main/assets/public/`.
- **Build blocked:** no Java Runtime on this machine (Apple JDK missing; `/usr/libexec/java_home -V` and `java -version` both fail). Install any JDK 17+ (e.g. `brew install openjdk@17`) then run:
  ```bash
  cd android && ./gradlew assembleDebug    # smoke
  cd android && ./gradlew bundleRelease    # AAB for Play (requires signing config)
  ```
- Remaining store-side items (human/platform gates): JDK install above, signing keystore, Play Console data-safety section, screenshots, age/content ratings.

## PWA fallback

The web build in `public/` is fully offline-capable (SW + offline deterministic table in `game.js`) and shippable today over any static host; both native bundles render this same web surface.
