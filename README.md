# dc-gen-android

Semi-automatic Discord account generator. Drives the official Discord Android
app inside Android Studio AVDs on Apple Silicon via `adb` + `uiautomator2`, with
optional `mitmproxy` token capture.

    ./run.sh dcgen.py doctor                      # preflight
    ./run.sh dcgen.py setup --apk ~/apk/discord.xapk
    ./run.sh dcgen.py recon                       # dump the screens
    ./run.sh dcgen.py run -n 1 --windowed         # one account, watch it
    ./run.sh dcgen.py run -n 2 --batch 2          # park 2, verify 2, repeat
    ./run.sh dcgen.py finalize                    # verify anything in pending.txt

Outputs (under `outputs/`): `tokens.txt`, `evs.txt`, `pending.txt`,
`locked.txt`, `report.csv`, `logs/`, `state/`, `shots/`.

## How a run works

Batch model. N accounts are registered and parked in `EMAIL_VERIFY_PENDING`,
then you verify all N in one sitting, then they are promoted to `evs.txt` and the
next batch starts. A crash between promote and drop can never duplicate a line -
promotion is idempotent by email.

The hCaptcha is **human by default**: the worker beeps, parks, and carries on the
moment the wrapper disappears. Set `captcha.mode: auto` to use the accessibility
text-challenge solver in `core/captcha.py`.

Email verification is manual (`verify_mode: paste`) unless `sms.enabled`.

## Layout

    dcgen.py          entry point: setup / doctor / recon / run / finalize / resume
    core/             orchestrator, state machine, human gates, http, token, captcha
    device/           driver, selector engine, picker, identity, ocr
    flows/            register, email_verify, phone_wall
    backends/         the AVD backend (one protocol, no backend code elsewhere)
    configs/          config.example.yaml, selectors.yaml, apk.json
    tools/            selftest, ca_install, apk_pin

## Requirements

macOS on Apple Silicon, Python 3.12 (the committed `.venv`), Android
command-line tools + a system image, `tesseract`, and optionally `scrcpy`
(for the human captcha window) and `mitmproxy`.

Always use `./run.sh` - it pins `.venv/bin/python`. Bare `python3` is a
different interpreter and will litter the tree with foreign `.pyc` files.