import os
import calendar
from datetime import datetime, date
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

app.config["MAIL_SERVER"]         = os.getenv("MAIL_SERVER", "smtp.gmail.com")
app.config["MAIL_PORT"]           = int(os.getenv("MAIL_PORT", 587))
app.config["MAIL_USE_TLS"]        = os.getenv("MAIL_USE_TLS", "True") == "True"
app.config["MAIL_USERNAME"]       = os.getenv("MAIL_USERNAME")
app.config["MAIL_PASSWORD"]       = os.getenv("MAIL_PASSWORD")
app.config["MAIL_DEFAULT_SENDER"] = os.getenv("MAIL_DEFAULT_SENDER", os.getenv("MAIL_USERNAME"))

TIER_EMAILS = {
    1: os.getenv("TIER1_EMAIL", "tier1@example.com"),
    2: os.getenv("TIER2_EMAIL", "tier2@example.com"),
    3: os.getenv("TIER3_EMAIL", "tier3@example.com"),
}
TIER_WHATSAPP = {
    1: os.getenv("TIER1_WHATSAPP"),
    2: os.getenv("TIER2_WHATSAPP"),
    3: os.getenv("TIER3_WHATSAPP"),
}
TIER_LABELS  = {1: "Loan Officer I", 2: "Loan Officer 2", 3: "Loan Officer 3"}
CURRENCY     = "UGX"
LOAN_TYPES   = ["Personal", "Business", "Mortgage", "Auto", "Education", "Asset Finance"]
REPAYMENT_PERIODS = [3, 6, 12, 18, 24, 36, 48, 60]
DEFAULT_PENALTY_RATE = float(os.getenv("DEFAULT_PENALTY_RATE", 5.0))

STATUS_COLORS = {
    "Pending Tier 1": "#d97706",
    "Pending Tier 2": "#0891b2",
    "Pending Tier 3": "#1B6B35",
    "Approved":       "#15803d",
    "Rejected":       "#dc2626",
}

try:
    from twilio.rest import Client as TwilioClient
    TWILIO_ENABLED = bool(os.getenv("TWILIO_ACCOUNT_SID"))
except ImportError:
    TWILIO_ENABLED = False

db     = SQLAlchemy(app)
mail   = Mail(app)
signer = URLSafeTimedSerializer(app.config["SECRET_KEY"])


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────

class LoanApplication(db.Model):
    id                = db.Column(db.Integer, primary_key=True)
    applicant_name    = db.Column(db.String(120), nullable=False)
    applicant_email   = db.Column(db.String(120), nullable=False)
    applicant_phone   = db.Column(db.String(30))
    loan_type         = db.Column(db.String(60),  nullable=False)
    amount            = db.Column(db.Float,        nullable=False)
    interest_rate     = db.Column(db.Float,        nullable=False)
    processing_fees   = db.Column(db.Float,        nullable=False)
    repayment_period  = db.Column(db.Integer)          # months
    purpose           = db.Column(db.Text)
    penalty_rate      = db.Column(db.Float, default=DEFAULT_PENALTY_RATE)

    status            = db.Column(db.String(40), default="Pending Tier 1")
    current_tier      = db.Column(db.Integer,    default=1)
    submitted_at      = db.Column(db.DateTime,   default=datetime.utcnow)
    disbursement_date = db.Column(db.Date)

    tier1_action  = db.Column(db.String(10))
    tier1_comment = db.Column(db.Text)
    tier1_at      = db.Column(db.DateTime)
    tier2_action  = db.Column(db.String(10))
    tier2_comment = db.Column(db.Text)
    tier2_at      = db.Column(db.DateTime)
    tier3_action  = db.Column(db.String(10))
    tier3_comment = db.Column(db.Text)
    tier3_at      = db.Column(db.DateTime)

    schedule = db.relationship(
        "LoanRepaymentSchedule", backref="loan", lazy=True,
        order_by="LoanRepaymentSchedule.installment_number",
        cascade="all, delete-orphan",
    )
    deposits = db.relationship(
        "LoanDeposit", backref="loan", lazy=True,
        order_by="LoanDeposit.deposit_date",
        cascade="all, delete-orphan",
    )

    @property
    def monthly_emi(self):
        if not self.repayment_period or self.repayment_period == 0:
            return 0
        return calculate_emi(self.amount, self.interest_rate, self.repayment_period)

    @property
    def total_repayable(self):
        return round(self.monthly_emi * self.repayment_period, 2) if self.repayment_period else 0

    @property
    def total_paid(self):
        return round(sum(s.amount_paid or 0 for s in self.schedule), 2)

    @property
    def total_outstanding(self):
        return round(sum(
            max((s.amount_due or 0) - (s.amount_paid or 0), 0)
            for s in self.schedule
        ), 2)

    @property
    def total_penalties(self):
        return round(sum(s.penalty_amount or 0 for s in self.schedule), 2)


class LoanRepaymentSchedule(db.Model):
    id                  = db.Column(db.Integer, primary_key=True)
    loan_id             = db.Column(db.Integer, db.ForeignKey("loan_application.id"), nullable=False)
    installment_number  = db.Column(db.Integer, nullable=False)
    due_date            = db.Column(db.Date,    nullable=False)
    amount_due          = db.Column(db.Float,   nullable=False)
    principal_component = db.Column(db.Float)
    interest_component  = db.Column(db.Float)
    amount_paid         = db.Column(db.Float,   default=0.0)
    payment_date        = db.Column(db.DateTime)
    status              = db.Column(db.String(20), default="Pending")  # Pending|Paid|Overdue|Partial
    penalty_amount      = db.Column(db.Float,   default=0.0)


class LoanDeposit(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    loan_id      = db.Column(db.Integer, db.ForeignKey("loan_application.id"), nullable=False)
    amount       = db.Column(db.Float,   nullable=False)
    deposit_date = db.Column(db.Date,    nullable=False)
    reference    = db.Column(db.String(100))
    notes        = db.Column(db.Text)
    recorded_at  = db.Column(db.DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────────────
# Finance helpers
# ─────────────────────────────────────────────────────────────────────────────

def add_months(source_date, months):
    """Add months to a date, clamping to last day of month if needed."""
    month = source_date.month - 1 + months
    year  = source_date.year + month // 12
    month = month % 12 + 1
    day   = min(source_date.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def calculate_emi(principal, annual_rate_pct, months):
    if months == 0:
        return 0
    r = annual_rate_pct / 100 / 12
    if r == 0:
        return round(principal / months, 2)
    return round(principal * r * (1 + r) ** months / ((1 + r) ** months - 1), 2)


def generate_repayment_schedule(loan):
    LoanRepaymentSchedule.query.filter_by(loan_id=loan.id).delete()
    r       = loan.interest_rate / 100 / 12
    n       = loan.repayment_period
    balance = loan.amount

    emi = calculate_emi(loan.amount, loan.interest_rate, n)

    for i in range(1, n + 1):
        due_date     = add_months(loan.disbursement_date, i)
        interest_c   = round(balance * r, 2)
        principal_c  = round(emi - interest_c, 2)

        if i == n:                          # last installment — clear remainder
            principal_c = round(balance, 2)

        amount_due = round(principal_c + interest_c, 2)
        balance    = round(balance - principal_c, 2)

        db.session.add(LoanRepaymentSchedule(
            loan_id=loan.id,
            installment_number=i,
            due_date=due_date,
            amount_due=amount_due,
            principal_component=principal_c,
            interest_component=interest_c,
        ))
    db.session.commit()


def refresh_schedule_status(loan):
    today = date.today()
    for s in loan.schedule:
        if s.status == "Paid":
            continue
        outstanding = round((s.amount_due or 0) - (s.amount_paid or 0), 2)
        if outstanding <= 0:
            s.status         = "Paid"
            s.penalty_amount = 0.0
        elif s.due_date < today:
            s.status = "Overdue"
            days_overdue     = (today - s.due_date).days
            months_overdue   = days_overdue / 30
            s.penalty_amount = round(outstanding * (loan.penalty_rate / 100) * months_overdue, 2)
        elif (s.amount_paid or 0) > 0:
            s.status = "Partial"
        else:
            s.status = "Pending"
    db.session.commit()


def apply_deposit_to_schedule(loan, deposit_amount):
    """Allocate deposit to oldest unpaid installments first. Returns unallocated surplus."""
    remaining = deposit_amount
    for s in sorted(loan.schedule, key=lambda x: x.installment_number):
        if s.status == "Paid" or remaining <= 0:
            continue
        outstanding = round((s.amount_due or 0) - (s.amount_paid or 0), 2)
        payment     = min(remaining, outstanding)
        s.amount_paid  = round((s.amount_paid or 0) + payment, 2)
        remaining      = round(remaining - payment, 2)
        if s.amount_paid >= s.amount_due:
            s.status       = "Paid"
            s.payment_date = datetime.utcnow()
            s.penalty_amount = 0.0
        else:
            s.status = "Partial"
    db.session.commit()
    return remaining


# ─────────────────────────────────────────────────────────────────────────────
# Notification helpers
# ─────────────────────────────────────────────────────────────────────────────

def _send_whatsapp(to_number, body):
    if not TWILIO_ENABLED or not to_number:
        return
    try:
        client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
        client.messages.create(
            from_=f"whatsapp:{os.getenv('TWILIO_WHATSAPP_FROM', '+14155238886')}",
            body=body,
            to=f"whatsapp:{to_number}",
        )
    except Exception as e:
        app.logger.error("WhatsApp error: %s", e)


def _tier_whatsapp_body(loan, tier, approve_url, reject_url):
    period_str = f"{loan.repayment_period} months" if loan.repayment_period else "N/A"
    emi_str    = f"{CURRENCY} {loan.monthly_emi:,.0f}" if loan.repayment_period else "N/A"
    return (
        f"🏦 *Infinitive Building Society — Loan Approval Required*\n\n"
        f"You have a loan pending your review as *{TIER_LABELS[tier]}* (Tier {tier} of 3).\n\n"
        f"*Applicant:* {loan.applicant_name}\n"
        f"*Loan Type:* {loan.loan_type}\n"
        f"*Amount:* {CURRENCY} {loan.amount:,.0f}\n"
        f"*Interest Rate:* {loan.interest_rate}% p.a.\n"
        f"*Repayment Period:* {period_str}\n"
        f"*Monthly EMI:* {emi_str}\n"
        f"*Processing Fees:* {CURRENCY} {loan.processing_fees:,.0f}\n\n"
        f"Click the link to review:\n"
        f"✅ Approve: {approve_url}\n"
        f"❌ Reject:  {reject_url}\n\n"
        f"_Links expire in 7 days._\n"
        f"_Turning Goals Into Wealth._"
    )


def send_tier_notifications(loan, tier):
    approve_token = signer.dumps({"loan_id": loan.id, "tier": tier, "action": "approve"})
    reject_token  = signer.dumps({"loan_id": loan.id, "tier": tier, "action": "reject"})
    approve_url   = url_for("review_action", token=approve_token, _external=True)
    reject_url    = url_for("review_action", token=reject_token,  _external=True)

    # Email
    msg = Message(
        subject=f"[Action Required] Loan #{loan.id} — {TIER_LABELS[tier]} Approval",
        recipients=[TIER_EMAILS[tier]],
        html=render_template(
            "email/tier_request.html",
            loan=loan, tier=tier, tier_label=TIER_LABELS[tier],
            approve_url=approve_url, reject_url=reject_url,
            currency=CURRENCY,
        ),
    )
    mail.send(msg)

    # WhatsApp
    _send_whatsapp(
        TIER_WHATSAPP[tier],
        _tier_whatsapp_body(loan, tier, approve_url, reject_url),
    )


def send_applicant_notification(loan):
    msg = Message(
        subject=f"Your Loan Application #{loan.id} — {loan.status}",
        recipients=[loan.applicant_email],
        html=render_template("email/applicant_result.html", loan=loan, currency=CURRENCY),
    )
    mail.send(msg)


def send_overdue_whatsapp(loan, installment):
    if not loan.applicant_phone:
        return
    body = (
        f"⚠️ *IBS Loan Payment Overdue*\n\n"
        f"Dear {loan.applicant_name},\n\n"
        f"Your installment #{installment.installment_number} for loan #{loan.id} "
        f"was due on {installment.due_date.strftime('%d %b %Y')} and is now overdue.\n\n"
        f"*Amount Due:* {CURRENCY} {installment.amount_due:,.0f}\n"
        f"*Amount Paid:* {CURRENCY} {installment.amount_paid or 0:,.0f}\n"
        f"*Penalty Accrued:* {CURRENCY} {installment.penalty_amount:,.0f}\n\n"
        f"Please make your payment promptly to avoid further penalties.\n"
        f"_Infinitive Building Society_"
    )
    _send_whatsapp(loan.applicant_phone, body)


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        try:
            amount          = float(request.form["amount"])
            interest_rate   = float(request.form["interest_rate"])
            processing_fees = float(request.form["processing_fees"])
            repayment_period = int(request.form["repayment_period"])
        except (ValueError, KeyError):
            flash("Please fill in all numeric fields correctly.", "danger")
            return redirect(url_for("index"))

        loan = LoanApplication(
            applicant_name   = request.form["applicant_name"].strip(),
            applicant_email  = request.form["applicant_email"].strip().lower(),
            applicant_phone  = request.form.get("applicant_phone", "").strip() or None,
            loan_type        = request.form["loan_type"],
            amount           = amount,
            interest_rate    = interest_rate,
            processing_fees  = processing_fees,
            repayment_period = repayment_period,
            purpose          = request.form.get("purpose", "").strip(),
            penalty_rate     = DEFAULT_PENALTY_RATE,
        )
        db.session.add(loan)
        db.session.commit()

        try:
            send_tier_notifications(loan, tier=1)
            flash(f"Application #{loan.id} submitted. Loan Officer I has been notified by email & WhatsApp.", "success")
        except Exception as e:
            app.logger.error("Notification error: %s", e)
            flash(f"Application #{loan.id} saved but notifications failed: {e}", "warning")

        return redirect(url_for("dashboard"))

    return render_template("index.html", loan_types=LOAN_TYPES,
                           repayment_periods=REPAYMENT_PERIODS, currency=CURRENCY)


@app.route("/dashboard")
def dashboard():
    loans = LoanApplication.query.order_by(LoanApplication.submitted_at.desc()).all()
    # Refresh penalty status for all active loans
    for loan in loans:
        if loan.schedule:
            refresh_schedule_status(loan)
    return render_template("dashboard.html", loans=loans,
                           status_colors=STATUS_COLORS, currency=CURRENCY)


@app.route("/loan/<int:loan_id>")
def loan_detail(loan_id):
    loan = LoanApplication.query.get_or_404(loan_id)
    if loan.schedule:
        refresh_schedule_status(loan)
    return render_template("loan_detail.html", loan=loan,
                           tier_labels=TIER_LABELS, status_colors=STATUS_COLORS,
                           currency=CURRENCY, today=date.today())


@app.route("/loan/<int:loan_id>/disburse", methods=["POST"])
def disburse_loan(loan_id):
    loan = LoanApplication.query.get_or_404(loan_id)
    if loan.status != "Approved":
        flash("Only fully approved loans can be disbursed.", "danger")
        return redirect(url_for("loan_detail", loan_id=loan_id))

    raw_date = request.form.get("disbursement_date")
    try:
        loan.disbursement_date = date.fromisoformat(raw_date)
    except (ValueError, TypeError):
        flash("Invalid disbursement date.", "danger")
        return redirect(url_for("loan_detail", loan_id=loan_id))

    db.session.commit()
    generate_repayment_schedule(loan)
    flash(f"Loan disbursed on {loan.disbursement_date.strftime('%d %b %Y')}. "
          f"Repayment schedule ({loan.repayment_period} instalments) generated.", "success")
    return redirect(url_for("loan_detail", loan_id=loan_id))


@app.route("/loan/<int:loan_id>/deposit", methods=["POST"])
def record_deposit(loan_id):
    loan = LoanApplication.query.get_or_404(loan_id)
    if not loan.disbursement_date:
        flash("Loan has not been disbursed yet.", "danger")
        return redirect(url_for("loan_detail", loan_id=loan_id))

    try:
        amount       = float(request.form["amount"])
        deposit_date = date.fromisoformat(request.form["deposit_date"])
    except (ValueError, KeyError):
        flash("Invalid deposit details.", "danger")
        return redirect(url_for("loan_detail", loan_id=loan_id))

    deposit = LoanDeposit(
        loan_id      = loan.id,
        amount       = amount,
        deposit_date = deposit_date,
        reference    = request.form.get("reference", "").strip() or None,
        notes        = request.form.get("notes", "").strip() or None,
    )
    db.session.add(deposit)
    db.session.commit()

    surplus = apply_deposit_to_schedule(loan, amount)
    if surplus > 0:
        flash(f"Deposit of {CURRENCY} {amount:,.0f} recorded. "
              f"Surplus of {CURRENCY} {surplus:,.0f} not yet allocated to future instalments.", "info")
    else:
        flash(f"Deposit of {CURRENCY} {amount:,.0f} recorded and applied to repayment schedule.", "success")

    return redirect(url_for("loan_detail", loan_id=loan_id))


@app.route("/penalties")
def penalties():
    loans = LoanApplication.query.filter(
        LoanApplication.disbursement_date.isnot(None)
    ).all()
    for loan in loans:
        refresh_schedule_status(loan)

    overdue_items = []
    for loan in loans:
        for s in loan.schedule:
            if s.status == "Overdue":
                overdue_items.append((loan, s))

    overdue_items.sort(key=lambda x: x[1].due_date)
    return render_template("penalties.html", overdue_items=overdue_items, currency=CURRENCY)


@app.route("/review/<token>", methods=["GET", "POST"])
def review_action(token):
    try:
        data = signer.loads(token, max_age=7 * 24 * 3600)
    except SignatureExpired:
        abort(410)
    except BadSignature:
        abort(400)

    loan   = LoanApplication.query.get_or_404(data["loan_id"])
    tier   = data["tier"]
    action = data["action"]

    if loan.current_tier != tier or loan.status in ("Approved", "Rejected"):
        return render_template("review_done.html", loan=loan, already_processed=True)

    if request.method == "POST":
        comment = request.form.get("comment", "").strip()
        now     = datetime.utcnow()

        setattr(loan, f"tier{tier}_action",  action)
        setattr(loan, f"tier{tier}_comment", comment)
        setattr(loan, f"tier{tier}_at",      now)

        if action == "reject":
            loan.status = "Rejected"
            db.session.commit()
            try:
                send_applicant_notification(loan)
            except Exception as e:
                app.logger.error("Mail error: %s", e)
            return render_template("review_done.html", loan=loan, action=action)

        next_tier = tier + 1
        if next_tier > 3:
            loan.status = "Approved"
            db.session.commit()
            try:
                send_applicant_notification(loan)
            except Exception as e:
                app.logger.error("Mail error: %s", e)
        else:
            loan.current_tier = next_tier
            loan.status       = f"Pending Tier {next_tier}"
            db.session.commit()
            try:
                send_tier_notifications(loan, tier=next_tier)
            except Exception as e:
                app.logger.error("Notification error: %s", e)

        return render_template("review_done.html", loan=loan, action=action)

    return render_template(
        "review_confirm.html",
        loan=loan, tier=tier, tier_label=TIER_LABELS[tier],
        action=action, token=token, currency=CURRENCY,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap DB
# ─────────────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
