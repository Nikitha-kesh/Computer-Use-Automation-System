"""
A tiny, self-contained Flask app that stands in for a legacy bank
back-office screen: look up a member, then open a new sub-account for
them, reaching a confirmation screen.

This exists as a network-free test fixture (used by tests/ to exercise the
replay engine, safety layer, and escalation flow with a *real* Playwright
browser but no external site dependency) and, optionally, as an offline
target you can point the discovery agent at instead of demoqa.com.

Deliberately includes the "realistic environment" properties called out in
the brief:
  - A one-time cookie-consent interstitial (recoverable condition).
  - A "member not found" business outcome (not a crash).
  - A validation business outcome ("must accept terms" / "deposit too low").
  - A custom, non-native dropdown for account type (no real <select>),
    mimicking a component-library control rather than clean semantic HTML.
"""

from __future__ import annotations

import random
import string

from flask import Flask, redirect, render_template_string, request, session, url_for

app = Flask(__name__)
app.secret_key = "dev-only-not-a-real-secret"

MEMBERS = {
    "12345": {"name": "Jordan Lee", "email": "jordan.lee@example.com"},
    "67890": {"name": "Priya Raman", "email": "priya.raman@example.com"},
}
ACCOUNTS: dict[str, dict] = {}

BASE = """
<!doctype html><html><head><title>{{ title }}</title></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 40px auto;">
{% if show_cookie_banner %}
<div id="cookie-banner" style="background:#333;color:#fff;padding:12px;margin-bottom:16px;">
  We use cookies. <button id="cookie-accept" type="button">Accept</button>
</div>
{% endif %}
{{ body|safe }}
</body></html>
"""

LOOKUP_BODY = """
<h1>Member Lookup</h1>
{% if error %}<div id="error-banner" style="color:red;">{{ error }}</div>{% endif %}
<form method="post" action="/lookup">
  <label for="member_id">Member ID</label>
  <input id="member_id" name="member_id" type="text" />
  <button type="submit">Look Up Member</button>
</form>
"""

OPEN_ACCOUNT_BODY = """
<h1>Open New Sub-Account</h1>
{% if error %}<div id="error-banner" style="color:red;">{{ error }}</div>{% endif %}
<form method="post" action="/open-account">
  <input type="hidden" name="member_id" value="{{ member_id }}" />
  <label for="full_name">Account Holder Name</label>
  <input id="full_name" name="full_name" type="text" value="{{ name }}" readonly />
  <label for="email">Email</label>
  <input id="email" name="email" type="email" value="{{ email }}" readonly />

  <label id="account_type_label">Account Type</label>
  <div id="account_type_display" tabindex="0" role="combobox" aria-label="Account Type"
       style="border:1px solid #ccc;padding:6px;width:200px;cursor:pointer;"
       onclick="document.getElementById('account_type_options').style.display='block'">
    Select account type...
  </div>
  <div id="account_type_options" style="display:none;border:1px solid #ccc;width:200px;">
    <div role="option" style="padding:6px;cursor:pointer;"
         onclick="selectAccountType('Savings')">Savings</div>
    <div role="option" style="padding:6px;cursor:pointer;"
         onclick="selectAccountType('Checking')">Checking</div>
    <div role="option" style="padding:6px;cursor:pointer;"
         onclick="selectAccountType('Money Market')">Money Market</div>
  </div>
  <input type="hidden" id="account_type" name="account_type" value="" />

  <label for="initial_deposit">Initial Deposit (USD)</label>
  <input id="initial_deposit" name="initial_deposit" type="number" />

  <label for="opened_date">Date Opened</label>
  <input id="opened_date" name="opened_date" type="date" />

  <div>
    <input id="terms" name="terms" type="checkbox" />
    <label for="terms">I confirm the account holder has agreed to the terms</label>
  </div>

  <button id="submit-account" type="submit">Submit</button>
</form>
<script>
function selectAccountType(v) {
  document.getElementById('account_type').value = v;
  document.getElementById('account_type_display').textContent = v;
  document.getElementById('account_type_options').style.display = 'none';
}
document.addEventListener('click', function(e) {
  if (e.target && e.target.id === 'cookie-accept') {
    document.getElementById('cookie-banner').remove();
  }
});
</script>
"""

CONFIRMATION_BODY = """
<h1>Account Opened</h1>
<div id="confirmation-panel" role="region" aria-label="Account confirmation panel">
  <p>Your new sub-account has been created.</p>
  <table>
    <tr><td>Account Number</td><td id="account-number" role="text" aria-label="Account Number value">{{ account_number }}</td></tr>
    <tr><td>Account Holder</td><td id="confirm-holder" role="text" aria-label="Account Holder value">{{ name }}</td></tr>
    <tr><td>Account Type</td><td id="confirm-type" role="text" aria-label="Account Type value">{{ account_type }}</td></tr>
    <tr><td>Initial Deposit</td><td id="confirm-deposit" role="text" aria-label="Initial Deposit value">${{ deposit }}</td></tr>
  </table>
</div>
"""


def _render(body_tpl: str, title: str, show_cookie_banner: bool = False, **ctx):
    body = render_template_string(body_tpl, **ctx)
    return render_template_string(BASE, title=title, body=body, show_cookie_banner=show_cookie_banner)


@app.route("/", methods=["GET"])
def index():
    first_visit = "cookie_seen" not in session
    session["cookie_seen"] = True
    return _render(LOOKUP_BODY, "Member Lookup", show_cookie_banner=first_visit, error=None)


@app.route("/lookup", methods=["POST"])
def lookup():
    member_id = request.form.get("member_id", "").strip()
    if member_id not in MEMBERS:
        return _render(LOOKUP_BODY, "Member Lookup", error=f"Member not found: {member_id}")
    return redirect(url_for("open_account_form", member_id=member_id))


@app.route("/open-account", methods=["GET"])
def open_account_form():
    member_id = request.args.get("member_id", "")
    member = MEMBERS.get(member_id)
    if not member:
        return _render(LOOKUP_BODY, "Member Lookup", error=f"Member not found: {member_id}")
    return _render(
        OPEN_ACCOUNT_BODY, "Open New Sub-Account",
        member_id=member_id, name=member["name"], email=member["email"], error=None,
    )


@app.route("/open-account", methods=["POST"])
def open_account_submit():
    member_id = request.form.get("member_id", "")
    member = MEMBERS.get(member_id)
    if not member:
        return _render(LOOKUP_BODY, "Member Lookup", error=f"Member not found: {member_id}")

    if request.form.get("terms") != "on":
        return _render(
            OPEN_ACCOUNT_BODY, "Open New Sub-Account", member_id=member_id,
            name=member["name"], email=member["email"],
            error="You must confirm the account holder has agreed to the terms.",
        )

    try:
        deposit = float(request.form.get("initial_deposit", "0"))
    except ValueError:
        deposit = 0.0
    if deposit < 25:
        return _render(
            OPEN_ACCOUNT_BODY, "Open New Sub-Account", member_id=member_id,
            name=member["name"], email=member["email"],
            error="Initial deposit must be at least $25.",
        )

    account_type = request.form.get("account_type", "")
    account_number = "".join(random.choices(string.digits, k=10))
    ACCOUNTS[account_number] = {
        "member_id": member_id, "name": member["name"], "type": account_type, "deposit": deposit,
    }
    return _render(
        CONFIRMATION_BODY, "Account Opened",
        account_number=account_number, name=member["name"], account_type=account_type, deposit=f"{deposit:.2f}",
    )


if __name__ == "__main__":
    app.run(port=5055, debug=False)
