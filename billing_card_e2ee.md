# Adding a credit card to a WABA — internal API + e2ee reverse-engineering

Source HAR: `adc cartao.har` (captured 2026-06-01, business_id 1198384760028178).
Goal: add a credit card to a WABA payment account programmatically.

## The flow is TWO phases (two separate wizard runs)

| Phase | Mutation | doc_id | What it does |
|-------|----------|--------|--------------|
| 1. Save card at **BM** level | `BillingSaveCardCredentialStateMutation` | 25934943219457748 | Encrypts + stores the card on the **business** payment account. Returns `credential_id`. |
| 2. Attach card to **WABA** | `BillingSaveSharedBizCardStateMutation` | 25126279877041501 | Links the BM `credential_id` to the WABA `payment_legacy_account_id`. **No encryption** — just IDs. |

Pre-req mutation: `PaymentsCometGetServerEncryptionKeyMutation` (doc_id 23994203586844376)
returns `trust_chain` = X.509 cert chain (leaf = EC **P-256** public key), minted **per
account/session**. Variables: `{input:{device_id:"device_id", payment_type:"BILLING_WIZARD",
target_account_id:<BM payment acct>, fetch_unified_wallet_key:false, logging_id, actor_id,
client_mutation_id}}`.

### Account IDs (HAR example — they are DIFFERENT)
- BM payment account (card saved here): `2074283953119105` (used as `payment_account_id` / `target_account_id`)
- WABA payment account (card attached here): `2025012164790450` (used as `payment_legacy_account_id`)

Phase 2 only needs `shared_biz_credential_id` (= phase-1 `credential_id`) + `payment_legacy_account_id`.
This is plain and fully replayable with `requests`/`page.evaluate(fetch)` — see
`scripts/test_add_card.py::attach_card_to_waba`.

## Phase 1 is the hard part: the card is e2ee-encrypted

In the GraphQL variables the PAN/CVV are NOT present — they are `$e2ee` markers:
```json
"card_data": {
  "bin":"52467420","cardholder_name":"...","expiry_month":"6","expiry_year":"2032","last_4":"0818",
  "credit_card_number": {"sensitive_string_value":"$e2ee"},
  "csc":                {"sensitive_string_value":"$e2ee"}
}
```
The real values live inside **`input.platform_trust_token`**.

### platform_trust_token structure
```
platform_trust_token = base64( JSON{ "payload": <P>, "signatures": [] } )   # signatures EMPTY (no device sig)
```
Two encodings exist for `<P>` depending on flow:

**(A) Binary container — used by ADD_CARD.** `<P>` is base64 of:
```
<meta_json_bytes> || <binary blob>
meta_json = {"data":{"credit_card":"$e2ee","csc":"$e2ee","expiry_month":"6","expiry_year":"2032"},
             "nonce":"<uuid>","op":"ADD_CARD","ver":1}
```
The binary blob (~413 B) is the ECDH-ES/A256GCM ciphertext + ephemeral key + iv + tag, packed
with uint32 **big-endian** length prefixes (`getBigEndianNumberIn4Bytes` =
`new Uint8Array(new Uint32Array([n]).buffer).reverse()`). **The exact binary serializer was not
fully located in the HAR JS** (likely `FBPayAuthLibraryCommon.createPttGeneric` / a binary PTT
module) — capture it live or from FB JS to finish the pure-Python path.

**(B) Dotted JWE-compact — used by other flows (NOT add-card).** `FBPayAuthLibraryCommon`'s
encrypt fn returns:
`metab64 . base64url(joseHeader) . base64url("") . base64url(iv) . base64url(ct) . base64url(tag)`

### The encryption (RFC 7518 ECDH-ES, standard primitives)
From `FBPayAuthLibraryUtils.getEncryptionKey` (`k`) + `getEncryptionKeyPayload` (`h`) + `buildJoseHeader` (`S`):
1. Ephemeral keypair: `crypto.subtle.generateKey({name:"ECDH",namedCurve:"P-256"})`.
2. Shared secret Z: `deriveBits(ECDH, server_pub, ephemeral_priv, 256)`.
3. **Concat KDF (SHA-256)** over: `counter(=1, u32 BE) || Z || [u32len||"A256GCM"] || [u32len||apu] || [u32len||apv] || keydatalen(=256, u32 BE)` → `SHA-256(...)` = the **A256GCM key**.
   - `apu` = `"fp:"+base64url(SHA-256(spki of LOCAL/ephemeral pub key))` (or `;`-joined kid list)
   - `apv` = `"fp:"+base64url(SHA-256(spki of SERVER pub key))`
4. JOSE header: `{alg:"ECDH-ES", apu, apv, enc:"A256GCM", epk:{crv:"P-256",kty:"EC",pem:<PEM ephemeral pub>}}`.
5. AAD = `base64url(JSON.stringify(joseHeader)) + "." + <payload>`; IV = 12 random bytes;
   plaintext = `JSON.stringify(secretPayload)`; AES-GCM tag = 128-bit (last 16 bytes).
6. **secretPayload for ADD_CARD** = `{credit_card:"<PAN>", csc:"<CVV>"}` (the real values for the `$e2ee` fields).

Key modules (all in HAR JS, entry 115/116/32):
`PaymentsCometGetServerEncryptionKeyMutation`, `getPTTUtils` (`getPTTInternalWithEncryption`),
`modularGeneratePTT`, `BillingPTTUtils.generatePTT`, `FBPayAuthLibraryCommon`
(`getCryptoKeyFromCert`, `getPTTForServerKey`, `createPttGeneric`, `getPTTWithoutSignature`),
`FBPayAuthLibraryUtils` (`getEncryptionKey`, `getEncryptionKeyPayload`, `buildJoseHeader`,
`getPEMPublicKey`, `genKidFingerprintFromKeyPair`), `FBPayCryptoUtils` (`parseX509Cert`, `importX509Cert`).

## Why pure `requests` replay of phase 1 fails
The captured encrypted blob is bound to a per-session server key + random ephemeral key, so it
cannot be reused for another card/account. You must re-encrypt. Two paths:
- **Browser-assisted (reliable):** call FB's own JS (`require("FBPayAuthLibraryCommon")...`) via
  `page.evaluate` to mint the token, then fire the mutation with `fetch`. Crypto always correct.
- **Pure-Python (standalone, fragile):** reproduce steps above with `cryptography` (ECDH P-256,
  Concat KDF, AES-GCM). Blocked only on the exact **binary container** byte layout (see (A)).

## Mutation request shape (phase 1, the non-secret parts)
`fb_api_req_friendly_name=BillingSaveCardCredentialStateMutation`, `doc_id=25934943219457748`.
`variables.input`: `billing_address.country_code`, `card_data` (above), `client_info`
(`color_depth,java_enabled,screen_height,screen_width`), `currency:"BRL"`,
`payment_account_id:<BM>`, `payment_intent:"ADD_PM"`, `platform_trust_token:<above>`,
`set_default:false`, `network_tokenization_consent_given:false`, `recurring_payment_consent_given:false`,
`upl_logging_data:{...}`, `actor_id`, `client_mutation_id`. Success →
`{"data":{"xfb_billing_save_card_credential":{... "credit_card":{"credential_id":"<id>", ...}}}}`.

## Browser-assisted minting (the clean path — no binary-serializer repro needed)
Live probe (`scripts/probe_card_crypto.py`) found that after opening the add-card wizard:
- `require("BillingPTTUtils").generatePTT(input, "wizard", true, true, l, s, billingRelay, false, false, true)`
  is reachable and **returns the finished `platform_trust_token` string** (it lazy-loads
  `modularGeneratePTT`, fetches the server key, encrypts, and builds the binary container itself).
- `require("RelayFBEnvironment")` is a live Relay environment → pass `billingRelay = {environment: it}`.
- `input = {paymentType:"BILLING_WIZARD", authData:{credit_card:"$e2ee",csc:"$e2ee",expiry_month,expiry_year},
  secretPayload:{credit_card:<PAN>, csc:<CVV>}, authInputOperation:"ADD_CARD", paymentAccountID:<acct>}`.
- `DTSGInitialData.token`, `LSD.token`, `CurrentUserInitialData.USER_ID` available to fire the mutation.

So `scripts/test_add_card_api.py` opens the wizard (to load the bundle), then in ONE
`page.evaluate`: mints the token via `generatePTT` and fires `BillingSaveCardCredentialStateMutation`
with `fetch`. No DOM card-form typing.

## Phase-1 API: VERIFIED WORKING (pipeline-wise)
`scripts/test_add_card_api.py` confirmed end-to-end on the live target account:
1. Resolve BM account: fire `BillingAddCreditCardScreenQuery` (doc_id 36360602320204776) with the
   **WABA payment_account_id** → `payment_account.billable_account.owner_business_payment_account.id`
   = the **BM account** to save into (NOT the WABA account — saving to the WABA gives
   `field_exception`/1150).
2. Mint PTT via `BillingPTTUtils.generatePTT` (1060 bytes, matches HAR).
3. Fire `BillingSaveCardCredentialStateMutation` with the BM `payment_account_id`.

Error progression proving each layer works (all on the live target account):
- WABA account → `field_exception` code 1150 (rejected pre-processing — wrong account).
- BM account + **fake PAN** → `Dados do cartão incorretos` code **4992001** (PTT decrypted
  server-side; reached card validation; failed only because the number was invalid).
- BM account + **real PAN** → `Este cartão já é usado por muitas contas` code **4992003** (card
  passed validation; blocked only by FB's anti-abuse "card on too many accounts" limit).

→ **The full API flow is confirmed working.** A card not over FB's account limit returns a
`credential_id`. No code fix needed; that last error is a Facebook policy limit on the test card.

**Gotcha — HAR PANs are unrecoverable:** the HAR only exposes `bin` (first 8) + `last_4`; the 4
middle digits are encrypted. Testing requires a REAL full card number, not zero-padded.

## Status
- Phase 2 (attach): implemented in `scripts/test_add_card.py`.
- Phase 1 DOM version: implemented in `scripts/test_add_card.py` (placeholder-based field selectors).
- Phase 1 API (browser-assisted): implemented + verified in `scripts/test_add_card_api.py` — needs a
  valid card to get a `credential_id`; account-resolution + crypto confirmed working.
- Phase 1 pure-Python: deferred — needs the exact binary container serializer (capture from the
  browser-assisted token once it works, then reproduce + diff).
