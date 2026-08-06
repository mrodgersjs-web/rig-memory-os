# Gothic Reckoning — Store Publication Requirements

Research date: 2026-08-05. Sources are Apple Developer and Google Play Console Help: the platforms that own the rules.

## What is ready in this repository

- Capacitor native shells exist in `ios/` and `android/` with stable app ID `com.rodgersintelligence.gothicreckoning`.
- The PWA has a standalone manifest, 512×512 maskable icon, offline tutorial fallback, and no location, contacts, photo, microphone, advertising, analytics, account, or tracking permission.
- The app therefore has a *no user data collected or shared* product posture **only while that remains true**. Adding a remote bot session, analytics, sign-in, crash reporting, or a third-party SDK changes the declarations below.

## Apple App Store

- [ ] **HUMAN-ONLY — enrollment and contracts.** The Apple Account Holder must enroll in the Apple Developer Program and accept the paid-app/agreements, tax, and banking terms in App Store Connect. An agent cannot execute those legal attestations.
- [ ] Create the App Store Connect record using bundle ID `com.rodgersintelligence.gothicreckoning`; upload an archive signed by the team’s Apple distribution certificate and provisioning profile.
- [ ] Add a public **Privacy Policy URL** both in App Store Connect and in the app. Apple requires the URL for iOS/macOS apps.
- [ ] Complete App Privacy. Declare every data type collected by this app or embedded third-party code. For the current offline build: select no data collected only after inspecting the final archive.
- [ ] Complete age rating and review notes. The game depicts murder/suspicion in a fictional social-deduction setting; the final rating must be set by App Store Connect’s questionnaire, not guessed in source code.
- [ ] Upload the required platform screenshots, 1024×1024 App Store icon, subtitle, description, keyword set, support URL, marketing URL (optional), and review contact details.
- [ ] Upload to TestFlight, test the release candidate, then submit for App Review.
- [ ] **HUMAN-ONLY — release.** The Account Holder must select manual/automatic release and submit. This is an external irreversible publication action.

**Official sources**

- Apple, [App Review Guidelines, 5.1.1 Privacy](https://developer.apple.com/app-store/review/guidelines/)
- Apple, [App Privacy Details](https://developer.apple.com/app-store/app-privacy-details/)
- Apple, [Manage app privacy in App Store Connect](https://developer.apple.com/help/app-store-connect/manage-app-information/manage-app-privacy/)
- Apple, [App Review distribution overview](https://developer.apple.com/distribute/app-review/)

## Google Play

- [ ] **HUMAN-ONLY — account/identity/payment.** The Play Console account owner must complete account verification and accept developer-distribution/payment agreements. This cannot be delegated to code.
- [ ] Build and sign an Android App Bundle (`.aab`) for package `com.rodgersintelligence.gothicreckoning`; enroll in Play App Signing and keep the upload key under account-holder control.
- [ ] Target Android API 36 for a new 2026 Play listing. Google’s current policy says new apps must target API 36 (Android 16) or higher; existing apps require API 35+ by 2026-08-31.
- [ ] Add a publicly accessible Privacy Policy URL in Play Console **and inside the app**, and complete the Data safety form. Current intended declaration: no data collected/shared — validate after final dependency scan.
- [ ] Complete App content: target audience, content rating questionnaire, ads declaration (no ads), app access (no restricted access for offline mode), and data-safety declaration.
- [ ] Upload store listing materials: app name, short/full description, 512×512 icon, feature graphic, phone screenshots, category, contact email, and privacy URL.
- [ ] Upload first to internal testing; exercise the install and offline flow on a physical device; promote through closed/open testing according to the account’s current testing requirement.
- [ ] **HUMAN-ONLY — release.** The account owner chooses production rollout and confirms the release. Store review outcome is outside this repository.

**Official sources**

- Google Play, [Target API level requirements](https://support.google.com/googleplay/android-developer/answer/11926878)
- Google Play, [User Data policy](https://support.google.com/googleplay/android-developer/answer/10144311)
- Google Play, [Data safety form](https://support.google.com/googleplay/android-developer/answer/10787469)
- Google Play, [Target audience and app content](https://support.google.com/googleplay/android-developer/answer/9867159)

## Release gate

The source can produce store-ready artifacts; it cannot truthfully claim publication without: (1) developer-account access, (2) a hosted privacy/support URL, (3) signing keys, (4) device-test evidence, and (5) an explicit owner release authorization. Those are intentional human/platform gates, not missing implementation.
