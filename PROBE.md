# Powershop preview and Pay later purchase

Requires Python 3.9 or later, with no additional packages or Home Assistant. From this repository run:

```sh
python3 tools/powershop_probe.py
```

Enter your Powershop email and the emailed one-time code in the terminal. If there are multiple accounts, select an account by its displayed ordinal. By default, the tool holds credentials only in memory and does not print tokens. Add `--cart` to read current cart lines, including the server quantityAvailable and whether each line matches a listed offer. Changed, removed or malformed lines are marked as unavailable. Preview modes do not change the cart or purchase anything. The tool sends an OTP only when invoked and after an email is entered.

The preview includes Staying Power, Special Pack, Future Pack, and EV Pack entries only. Missing, unavailable or malformed approved entries are counted as unknown. The returned `purchaseLimit` is displayed as a quantity, but whether it represents the current remaining allowance has not been verified. An absent redemption date is unknown. The preview is not an order, and eligibility must be confirmed in Powershop. Purchased-pack history is counted across pages, but the preview does not infer current allowance or recurring EV eligibility from it.

Run the offline tests with:

```sh
python3 -m unittest discover -s tools -p "test_*.py" -v
```

Failures identify the operation and, when available, the HTTP status without displaying response bodies or credentials. If a request fails, report the operation and status when troubleshooting.

To avoid entering an OTP on every run, opt in with:

```sh
python3 -B tools/powershop_probe.py --remember-login --cart
```

Use `--remember-login` on later runs as well. The tool stores only a Firebase refresh token in `~/.config/powershopnz-probe/session.json`, with a private directory and file. Treat that local file as a credential. A confirmed revoked login prompts for a new code; temporary refresh errors leave the saved login intact. Remove the saved login without contacting the service with:

```sh
python3 -B tools/powershop_probe.py --forget-login
```

Use `--select-pay-later` to read the cart and prepare a local Pay later preview. It checks the cart, listed offers, credit and quoted amount, then displays the proposed checkout route and amount. It does not submit the request. Payment selection follows the app contract; server checkout acceptance is not tested. If the server reports a resized item, adjusted quantity or version mismatch, the preview reads the catalogue and cart once more before validating. Removed items, persistent changes, inconsistent carts and zero amounts covered by credit cannot be prepared.

To purchase the current validated cart using Pay later, run:

```sh
python3 -B tools/powershop_probe.py --remember-login --purchase-pay-later
```

This is a real purchase. Review the cart and amount, then type the displayed phrase exactly, for example `PURCHASE 30.47`. The tool checks the cart again after confirmation and stops if it changed. A private purchase journal in `~/.config/powershopnz-probe/` records attempts and receipts. If the result is unknown or recording the receipt fails, check the Powershop app and purchase history before manually resolving the journal; the tool blocks further purchases while an attempt is unresolved. `--forget-login` does not remove that journal.
