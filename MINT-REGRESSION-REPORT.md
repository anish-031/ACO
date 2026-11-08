# Mint Engine Old-vs-New Regression Report

## Scope and evidence

- Old reference: `bots-source (1) (1).zip`, source and tests extracted read-only to a temporary directory.
- New target: the current `acobot/` working tree, backed up before edits to `../acobot.backup-before-mint-repair-20261104-0048`.
- `acobot.zip` does not exactly match the current working tree for `bot.py`, `sniper.py`, or `panels.py`; the working tree is the repair target.
- The old test archive contains 25 test modules. All 25 remain in the new test tree, which adds 13 modules; no old test module was dropped.
- No live/mainnet transaction was signed or broadcast; validation uses offline tests and mocks.

## Verified execution regressions and repairs

1. **Gated firing modes are removed and then overridden.**
   - Old `draft_kb` exposed `prefire`, `normal`, and `dual` for every mint.
   - New `draft_kb` omitted those controls for gated phases; new `schedule_draft` overwrote the mode with `normal`; and new `sniper.wait_and_fire` also forced normal mode when `authorization_at_fire` was true.
   - Restored all three controls and preserve the selected mode in scheduled records. The watcher may resolve/simulate authorization before the opening window, retries when missing/expired, and honors the selected prefire/normal/dual path only with valid transaction data.
   - The prefire trigger remains tied to the next block timestamp reaching the selected opening time; no authorization or calldata is fabricated to force an early transaction.

2. **Eligibility responses lose usable authorization.**
   - The old `eligible._verdict` treated proof/signature fields as `eligible=True`; `probe_sync` returned no payload. The new implementation separated an authorization status but still discarded the response.
   - Eligibility now reports only explicit boolean eligibility. It preserves any proof/signature/transaction payload separately. The resolver tries the existing supported eligibility endpoint candidates when no phase API is configured, then passes any response through transaction/context/phase validation and wallet simulation.

3. **SeaDrop authorization encoding uses the wrong NFT argument.**
   - New `mintapi._encode_seadrop_authorization` filled the second `address` argument with the wallet instead of the selected NFT contract.
   - The encoder now inserts the contract, wallet, phase kind, quantity, and actual endpoint parameters; it validates the decoded wallet/contract/method, stage index, price, fee recipient, quantity, and explicit payment value before simulation.

4. **Gated API transaction routing is too permissive in one direction and too restrictive in another.**
   - New `mint_router.build_transaction` rejected gated mints unless `_api_calldata` existed and returned API calldata before selector validation.
   - Routing now validates the selected phase’s exact SeaDrop method and requires an explicit value on gated transactions. `mintPublic` cannot be used for gated phases.

5. **Selected gated phase data is overwritten during scheduling.**
   - New `draft_phase_view` read selected display values, but `schedule_draft` overwrote gated price with zero and limit with `None`; `panels.mint_record` also converted missing prices to zero and omitted proof metadata.
   - Scheduling now preserves unknown transaction price as `None`, preserves display price separately, retains limits/windows/stage index/Merkle root/signer count/mint-site metadata, and uses the returned SeaDrop authorization’s price/value for transaction construction.

## Changes that are not regressions and must be retained

- New chain-ID verification and selected-chain validation in `rpc.py`/`mint_engine.py` prevent cross-chain transaction construction.
- New EIP-7702 and verified-ABI analysis in `mintread.py` extends analysis without replacing the existing SeaDrop reads.
- New transaction simulation, wallet/context checks, response expiration handling, and refusal to substitute public calldata for a gated phase are useful safety improvements.
- New RPC pooling/fan-out and parallel wallet preparation remain in place; neither old-vs-new source inspection nor tests establish a numeric broadcast-latency guarantee.
- The old project did not include a separate `mint_router.py`; its SeaDrop mint builder used `mintPublic` for gated phases as well. That behavior is not restored because it would bypass the required SeaDrop authorization method.

## Implemented repair

1. Kept the new project intact; no wholesale file replacement.
2. Kept eligibility and authorization separate while preserving response payloads.
3. Routed supported eligibility/API responses through strict transaction parsing and wallet-specific simulation on the selected chain.
4. Corrected SeaDrop authorization arguments and phase-specific route validation.
5. Restored all three firing modes with an authorization retry path and no invalid-transaction broadcast.
6. Preserved gated price/limit/stage metadata without inventing a zero price.
7. Added offline regression tests and ran the full local test suite.

## Validation

- Full offline test suite: **407 passed, 1 skipped, 12 subtests passed**.
- Pytest emitted an `asyncio_default_fixture_loop_scope` deprecation warning; it did not affect the test result.
- No live-chain transaction was signed or broadcast. Network-specific mint execution still requires separately configured RPC/API access and valid project authorization.

## Support boundary

This repair targets the existing EVM SeaDrop and simple custom-call paths. A response is usable only when an existing eligibility/API endpoint provides enough real transaction/authorization data for validation and simulation. Website inspection alone does not establish an API contract; no additional endpoint shapes are inferred beyond the eligibility candidates already present. Those responses are not trusted until parsing and on-chain simulation succeed. Solana remains isolated from the EVM transaction path.
