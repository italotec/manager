from flask import Blueprint, render_template
from flask_login import login_required

bp = Blueprint("billing", __name__)

@bp.route("/recharge", methods=["GET"])
@login_required
def recharge():
    # Por enquanto é só uma tela informativa.
    # Depois você pode integrar PIX / checkout aqui.
    return render_template("recharge.html", title="Recarregar saldo")
