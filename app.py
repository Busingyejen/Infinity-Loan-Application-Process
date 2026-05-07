import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, abort
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///loans.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

app.config["MAIL_SERVER"] = os.getenv("MAIL_SERVER", "smtp.gmail.com")
app.config["MAIL_PORT"] = int(os.getenv("MAIL_PORT", 587))
app.config["MAIL_USE_TLS"] = os.getenv("MAIL_USE_TLS", "True") == "True"
app.config["MAIL_USERNAME"] = os.getenv("MAIL_USERNAME")
app.config["MAIL_PASSWORD"] = os.getenv("MAIL_PASSWORD")
app.config["MAIL_DEFAULT_SENDER"] = os.getenv("MAIL_DEFAULT_SENDER", os.getenv("MAIL_USERNAME"))

TIER_EMAILS = {
    1: os.getenv("TIER1_EMAIL", "tier1@example.com"),
    2: os.getenv("TIER2_EMAIL", "tier2@example.com"),
    3: os.getenv("TIER3_EMAIL", "tier3@example.com"),
}
TIER_LABELS = {1: "Loan Officer I", 2: "Loan Officer 2", 3: "Loan Officer 3"}
CURRENCY = "UGX"

db = SQLAlchemy(app)
mail = Mail(app)
signer = URLSafeTimedSerializer(app.config["SECRET_KEY"])

LOAN_TYPES = ["Personal", "Business", "Mortgage", "Auto", "Education", "Asset Finance"]
STATUS_COLORS = {
    "Pending Tier 1": "warning",
    "Pending Tier 2": "info",
    "Pending Tier 3": "primary",
    "Approved": "success",
    "Rejected": "danger",
}


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class LoanApplication(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    applicant_name = db.Column(db.String(120), nullable=False)
    applicant_email = db.Column(db.String(120), nullable=False)
    loan_type = db.Column(db.String(60), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    interest_rate = db.Column(db.Float, nullable=False)
    processing_fees = db.Column(db.Float, nullable=False)
    purpose = db.Column(db.Text)
    status = db.Column(db.String(40), default="Pending Tier 1")
    current_tier = db.Column(db.Integer, default=1)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    tier1_action = db.Column(db.String(10))
    tier1_comment = db.Column(db.Text)
    tier1_at = db.Column(db.DateTime)
    tier2_action = db.Column(db.String(10))
    tier2_comment = db.Column(db.Text)
    tier2_at = db.Column(db.DateTime)
    tier3_action = db.Column(db.String(10))
    tier3_comment = db.Column(db.Text)
    tier3_at = db.Column(db.DateTime)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_token(loan_id: int, tier: int, action: str) -> str:
    return signer.dumps({"loan_id": loan_id, "tier": tier, "action": action})


def send_tier_email(loan: LoanApplication, tier: int):
    approve_token = make_token(loan.id, tier, "approve")
    reject_token = make_token(loan.id, tier, "reject")
    approve_url = url_for("review_action", token=approve_token, _external=True)
    reject_url = url_for("review_action", token=reject_token, _external=True)

    msg = Message(
        subject=f"[Action Required] Loan #{loan.id} — Tier {tier} Approval ({TIER_LABELS[tier]})",
        recipients=[TIER_EMAILS[tier]],
        html=render_template(
            "email/tier_request.html",
            loan=loan,
            tier=tier,
            tier_label=TIER_LABELS[tier],
            approve_url=approve_url,
            reject_url=reject_url,
            currency=CURRENCY,
        ),
    )
    mail.send(msg)


def send_applicant_email(loan: LoanApplication):
    msg = Message(
        subject=f"Your Loan Application #{loan.id} — {loan.status}",
        recipients=[loan.applicant_email],
        html=render_template("email/applicant_result.html", loan=loan, currency=CURRENCY),
    )
    mail.send(msg)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        try:
            amount = float(request.form["amount"])
            interest_rate = float(request.form["interest_rate"])
            processing_fees = float(request.form["processing_fees"])
        except ValueError:
            flash("Amount, interest rate, and processing fees must be numbers.", "danger")
            return redirect(url_for("index"))

        loan = LoanApplication(
            applicant_name=request.form["applicant_name"].strip(),
            applicant_email=request.form["applicant_email"].strip().lower(),
            loan_type=request.form["loan_type"],
            amount=amount,
            interest_rate=interest_rate,
            processing_fees=processing_fees,
            purpose=request.form.get("purpose", "").strip(),
        )
        db.session.add(loan)
        db.session.commit()

        try:
            send_tier_email(loan, tier=1)
            flash(f"Application #{loan.id} submitted. Awaiting Tier 1 approval.", "success")
        except Exception as e:
            app.logger.error("Mail error: %s", e)
            flash(
                f"Application #{loan.id} saved, but email could not be sent ({e}). "
                "Check your mail settings.",
                "warning",
            )

        return redirect(url_for("dashboard"))

    return render_template("index.html", loan_types=LOAN_TYPES, currency=CURRENCY)


@app.route("/dashboard")
def dashboard():
    loans = LoanApplication.query.order_by(LoanApplication.submitted_at.desc()).all()
    return render_template("dashboard.html", loans=loans, status_colors=STATUS_COLORS, currency=CURRENCY)


@app.route("/loan/<int:loan_id>")
def loan_detail(loan_id):
    loan = LoanApplication.query.get_or_404(loan_id)
    return render_template(
        "loan_detail.html", loan=loan, tier_labels=TIER_LABELS,
        status_colors=STATUS_COLORS, currency=CURRENCY
    )


@app.route("/review/<token>", methods=["GET", "POST"])
def review_action(token):
    try:
        data = signer.loads(token, max_age=7 * 24 * 3600)  # 7-day expiry
    except SignatureExpired:
        abort(410)  # Gone
    except BadSignature:
        abort(400)

    loan = LoanApplication.query.get_or_404(data["loan_id"])
    tier = data["tier"]
    action = data["action"]  # "approve" or "reject"

    # Guard: already processed at this tier or beyond
    if loan.current_tier != tier or loan.status in ("Approved", "Rejected"):
        return render_template("review_done.html", loan=loan, already_processed=True)

    if request.method == "POST":
        comment = request.form.get("comment", "").strip()
        now = datetime.utcnow()

        setattr(loan, f"tier{tier}_action", action)
        setattr(loan, f"tier{tier}_comment", comment)
        setattr(loan, f"tier{tier}_at", now)

        if action == "reject":
            loan.status = "Rejected"
            db.session.commit()
            try:
                send_applicant_email(loan)
            except Exception as e:
                app.logger.error("Mail error: %s", e)
            return render_template("review_done.html", loan=loan, action=action)

        # Approved at this tier — advance
        next_tier = tier + 1
        if next_tier > 3:
            loan.status = "Approved"
            loan.current_tier = tier
            db.session.commit()
            try:
                send_applicant_email(loan)
            except Exception as e:
                app.logger.error("Mail error: %s", e)
        else:
            loan.current_tier = next_tier
            loan.status = f"Pending Tier {next_tier}"
            db.session.commit()
            try:
                send_tier_email(loan, tier=next_tier)
            except Exception as e:
                app.logger.error("Mail error: %s", e)

        return render_template("review_done.html", loan=loan, action=action)

    # GET — show confirmation page
    return render_template(
        "review_confirm.html",
        loan=loan,
        tier=tier,
        tier_label=TIER_LABELS[tier],
        action=action,
        token=token,
        currency=CURRENCY,
    )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
