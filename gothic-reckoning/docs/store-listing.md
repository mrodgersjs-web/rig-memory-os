# Gothic Reckoning — Store Listing Metadata

Copy-paste-ready for App Store Connect and Google Play Console. Every field is sourced from the in-app content that already exists.

---

## Both platforms

### App name
**Gothic Reckoning**

### Subtitle (30 chars, Apple) / Short description (80 chars, Play)
**A werewolf tale for one soul** (25) / **Twelve souls. One wolf in the dark. Solve it before dawn.** (45)

### Category
Apple: **Games → Strategy** (primary), **Games → Puzzle** (secondary)
Play: **Game → Strategy → Werewolf** (nearest available)

### Keywords (Apple, 100 chars, comma-sep, no repeats)
werewolf,social deduction,story game,night,puzzle,twelve souls,god's eye,seer,witch,hunter
(94 chars)

### Age rating
Apple: **9+** (Frequent/Intense fantasy violence; no realistic violence, no profanity, no user-generated content)
Play: **Everyone 10+** (Fantasy Violence, Mild) — fill out the IARC questionnaire with: violence=mild/fantasy, user-generated-content=no, social=no, ads=no

### Privacy
Policy URL: **hosted at `/privacy.html` in the shipped build**. For the console forms, host a live URL — see "Human gate 3" below.
Data collection: **none** — zero accounts, zero analytics, zero ads, zero location/photo/contacts access. Apple Privacy Nutrition Label: mark everything "Not Collected".

### Content rating answers (Apple)
- Cartoon/Fantasy Violence: **Frequent**
- Realistic Violence: **Never**
- Horror/Fear Themes: **Infrequent/Mild**
- Profanity: **Never**
- Sexual Content: **Never**; Nudity: **Never**
- Mature/Suggestive Themes: **Infrequent/Mild**
- User-Generated Content: **No**
- Unrestricted Web Access: **No** (no external browsing in-app; API calls scoped to `/api/game/*` same-origin)

### Content rating answers (Play IARC)
- Violence: **Yes → Mild → Fantasy**
- Sexuality: **No**
- Language: **No**
- Drug Use: **No**
- User-Generated Content: **No**
- Social Features: **No**
- In-App Purchases: **No**
- Ads: **No**

---

## Apple App Store Connect

1. **App Information → Name:** Gothic Reckoning
2. **Subtitle:** A werewolf tale for one soul
3. **Category:** Games → Strategy (primary), Games → Puzzle (secondary)
4. **Keywords:** line above
5. **Privacy Policy URL:** hosted live URL from gate 3
6. **Age Rating:** answer the questionnaire as above → computed 9+
7. **Screenshots — required:**
   - 6.9" iPhone (1290×2796): `store-assets/apple-1-title.png`, `apple-2-game.png`, `apple-3-privacy.png`
   - 12.9" iPad (2048×2732): `store-assets/ipad-1-title.png`, `ipad-2-game.png`, `ipad-3-privacy.png`
   - 6.5" iPhone: Apple auto-downscales 1290×2796 → 1242×2688
8. **App Icon:** `store-assets/icon-1024.png` (1024×1024 RGB, opaque)
9. **Promotional Text (170 chars, optional):**
   "Twelve souls gather at the Reckoning. Four of them are wolves. Through three nights of night-fall, dawn, and judgment, name them before the village is gone."
10. **Description (4000 chars):**
    ```
    Gothic Reckoning is a werewolf tale for a single player.

    Twelve souls sit around a candle-lit table in the dark hours of the night. Four of them are wolves in disguise. Each night, the wolves kill one villager. Each day, the village votes to hang a suspect. Your job: name all four wolves before the village runs out of souls.

    Your table is full of characters the gods whisper to —
        • The Seer, who asks the Referee: "Is this soul a wolf?"
        • The Witch, who carries a poison and a cure.
        • The Hunter, who takes his killer down with him.
        • The Guard, who blocks one attack per night.
    You hold none of these cards. You are the Reckoning itself — the vote that decides who burns at dawn.

    The game plays in the style of 1990s story games: a dark gothic serif, a hand-drawn wooden table, a written ledger of the night's events, phases that march Night → Day → Vote → Dusk until the question is settled.

    Features
    • Full Werewolf loop for 12 players, 4 wolves
    • Classic role set: Villager, Werewolf, White Wolf King, Wolf Cub, Seer, Witch, Hunter, Guard, Idiot
    • Offline mode: plays entirely on-device, deterministic seed-based, no internet required
    • Play-as-any-soul or god-mode: take the Reckoning seat, or watch the twelve souls play themselves
    • Privacy-first: no accounts, no analytics, no ads, no social, no location access
    • PWA-capable: add to Home Screen to play offline

    The Reckoning is not a puzzle to optimize — it is a story to survive. Every run is a different night's tale.
    ```

---

## Google Play Console

1. **Store listing → App name:** Gothic Reckoning
2. **Short description (80):** Twelve souls. One wolf in the dark. Solve it before dawn.
3. **Full description (4000):** use the Apple description block above (Play allows same copy).
4. **Category:** Game → Strategy (Play has no "Werewolf" subcategory; add the tag `social deduction` in "Tags" if available)
5. **Privacy Policy URL:** hosted live URL from gate 3
6. **Graphics:**
   - App icon: `store-assets/icon-1024.png` → crop 512×512 with: `python3 -c "from PIL import Image; Image.open('icon-1024.png').resize((512,512), Image.Resampling.LANCZOS).save('play-icon.png')"`
   - Feature graphic: `store-assets/play-feature-graphic.png` (1024×500)
   - Phone screenshots (≥2, landscape): `store-assets/play-1-title.png`, `play-2-game.png`, `play-3-privacy.png` (1280×720)
   - Tablet: not required by Play for phone-only apps; Play shows phone screenshots on tablets if tablet listing isn't filled
7. **Data safety section:** answer every category "No" — the app collects nothing.
8. **Content rating (IARC questionnaire):** answers in the section above → **Everyone 10+**.
9. **Target audience:** 13+ per the violence questionnaire; COPPA box stays **unchecked**.
10. **Release:** internal → closed testing → production.

---

## Human gates — what only YOU can do

The following steps require accounts/credentials I cannot hold. These are the actual blockers between where we are and a published app:

| # | Platform | Gate | Cost | Time |
|---|----------|------|------|------|
| 1 | Apple | **Apple Developer Program enrollment** | $99/yr | instant–48h |
| 2 | Google | **Google Play Developer account** | $25 one-time | instant |
| 3 | Both | **Hosted privacy policy URL** | free (any static host) | 5 min |
| 4 | Apple | **App signing key** | free (auto-generated in Xcode) | 1 min |
| 5 | Google | **App signing key** | free (Google Play App Signing) | 1 min |
| 6 | Apple | **Upload build to App Store Connect** | free | ~15 min upload |
| 7 | Google | **Upload AAB to Play Console** | free | ~5 min upload |
| 8 | Both | **Review decision** | free | Apple 24–48h, Google hours–7 days |
| 9 | Both | **Release authorization** | free | 1 click each |

**Total cash outlay: $124** ($99 Apple + $25 Google). Everything else is account-level.

### Why can't I do it?

- Apple Developer enrollment requires your Apple ID, two-factor auth at your phone, and a payment card.
- Play Console requires your Google account and the $25 fee on a payment method.
- The signing keys generated on this machine are tied to my terminal session — you would need them on the machine Apple/Google will see.
- The privacy policy URL must be reachable from the public internet to the store reviewer — this can only be done by a real account holder pointing a real domain.
- Review responses (appeals, clarifications, screenshots after rejection) come back to the account email.

### What I'd hand you

1. A signed iOS Archive (already at `ios/App/App.xcodeproj`; after you sign it, `xcodebuild -exportArchive` produces the IPA)
2. A signed Android AAB (already built below; Play Console accepts it)
3. Store assets in `gothic-reckoning/docs/store-assets/`
4. The metadata above, ready to paste

The build itself is done. The review process is yours to run.
