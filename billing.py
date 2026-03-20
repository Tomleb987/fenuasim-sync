#!/usr/bin/env python3
"""
billing.py — FENUASIM
Complète main.py : confirme les commandes, crée et envoie les factures.

Usage :
  - Importer dans main.py : from billing import auto_invoice_order
  - Ou lancer seul : python billing.py  (traite toutes les commandes confirmées sans facture)

Dépendances : aucune (utilise xmlrpc standard)
"""

import os
import xmlrpc.client

# ─── CONFIG (mêmes variables que main.py) ─────────────────────────────────────
ODOO_URL      = os.getenv("ODOO_URL")
ODOO_DB       = os.getenv("ODOO_DB")
ODOO_USER     = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

# ─── CONNEXION ────────────────────────────────────────────────────────────────
common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
uid    = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
m      = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)

def call(model, method, args, kw=None):
    return m.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, args, kw or {})

# ─── ÉTAPE 1 : CONFIRMER UNE COMMANDE ────────────────────────────────────────
def confirm_order(order_id: int, expected_total: float = None) -> bool:
    """
    Confirme une sale.order (draft → sale).
    Vérifie optionnellement que le total Odoo correspond au montant attendu.
    Retourne True si confirmée, False sinon.
    """
    try:
        # Vérification du total si fourni
        if expected_total is not None:
            rec = call("sale.order", "read", [[order_id]], {"fields": ["amount_total", "state"]})[0]
            if rec["state"] == "sale":
                return True  # déjà confirmée
            total = float(rec["amount_total"])
            if abs(total - expected_total) > 0.05:
                print(f"  ⚠ Commande {order_id} : total Odoo={total:.2f} vs attendu={expected_total:.2f} — skip", flush=True)
                return False

        call("sale.order", "action_confirm", [[order_id]])
        print(f"  ✓ Commande {order_id} confirmée", flush=True)
        return True
    except Exception as e:
        print(f"  ✗ Erreur confirmation commande {order_id} : {e}", flush=True)
        return False


# ─── ÉTAPE 2 : CRÉER LA FACTURE ──────────────────────────────────────────────
def create_invoice(order_id: int) -> int | None:
    """
    Crée la facture client depuis une sale.order confirmée.
    Retourne l'ID de la facture créée (account.move), ou None si déjà existante.
    """
    try:
        # Vérifier si une facture existe déjà
        existing = call(
            "account.move", "search",
            [[
                ("invoice_origin", "like", _get_order_name(order_id)),
                ("move_type", "=", "out_invoice"),
                ("state", "!=", "cancel")
            ]]
        )
        if existing:
            print(f"  → Facture déjà existante pour commande {order_id} (invoice id={existing[0]})", flush=True)
            return existing[0]

        # Créer la facture via _create_invoices
        invoice_ids = call("sale.order", "_create_invoices", [[order_id]])
        if not invoice_ids:
            print(f"  ✗ Aucune facture créée pour commande {order_id}", flush=True)
            return None

        invoice_id = invoice_ids[0]
        print(f"  ✓ Facture {invoice_id} créée pour commande {order_id}", flush=True)
        return invoice_id

    except Exception as e:
        print(f"  ✗ Erreur création facture pour commande {order_id} : {e}", flush=True)
        return None


def _get_order_name(order_id: int) -> str:
    """Récupère le nom de la commande (ex: S00042)."""
    try:
        rec = call("sale.order", "read", [[order_id]], {"fields": ["name"]})[0]
        return rec["name"]
    except Exception:
        return str(order_id)


# ─── ÉTAPE 3 : VALIDER LA FACTURE ────────────────────────────────────────────
def validate_invoice(invoice_id: int) -> bool:
    """
    Passe la facture de brouillon à 'posted' (validée).
    Ajoute automatiquement la mention TVA 293B si absente.
    """
    try:
        rec = call("account.move", "read", [[invoice_id]], {"fields": ["state", "narration"]})[0]

        if rec["state"] == "posted":
            print(f"  → Facture {invoice_id} déjà validée", flush=True)
            return True

        # Ajouter la mention légale 293B si absente
        mention = "TVA non applicable — article 293B du CGI."
        narration = rec.get("narration") or ""
        if mention not in (narration or ""):
            call("account.move", "write", [[invoice_id]], {
                "vals": {"narration": f"{narration}\n{mention}".strip()}
            })

        # Valider la facture
        call("account.move", "action_post", [[invoice_id]])
        print(f"  ✓ Facture {invoice_id} validée (posted)", flush=True)
        return True

    except Exception as e:
        print(f"  ✗ Erreur validation facture {invoice_id} : {e}", flush=True)
        return False


# ─── ÉTAPE 4 : ENVOYER LA FACTURE PAR EMAIL ──────────────────────────────────
def send_invoice_by_email(invoice_id: int) -> bool:
    """
    Envoie la facture validée au client par email.
    Utilise le modèle email de facture standard d'Odoo.
    """
    try:
        rec = call(
            "account.move", "read",
            [[invoice_id]],
            {"fields": ["state", "partner_id", "name"]}
        )[0]

        if rec["state"] != "posted":
            print(f"  ⚠ Facture {invoice_id} non validée — envoi impossible", flush=True)
            return False

        partner_email = _get_partner_email(rec["partner_id"][0])
        if not partner_email or partner_email == "client@fenuasim.com":
            print(f"  ⚠ Facture {invoice_id} : email client manquant — envoi ignoré", flush=True)
            return False

        # Récupérer le template email de facture
        template_ids = call(
            "mail.template", "search",
            [[("model", "=", "account.move"), ("name", "ilike", "Invoice")]],
            {"limit": 1}
        )

        if template_ids:
            # Envoi via le template standard
            call(
                "mail.template", "send_mail",
                [template_ids[0], invoice_id],
                {"force_send": True}
            )
            print(f"  ✓ Facture {invoice_id} envoyée à {partner_email}", flush=True)
        else:
            # Fallback : envoi simple via message_post
            call("account.move", "message_post", [[invoice_id]], {
                "body": f"Veuillez trouver ci-joint votre facture {rec['name']}.<br/>TVA non applicable — art. 293B du CGI.",
                "subtype_xmlid": "mail.mt_comment",
                "partner_ids": [rec["partner_id"][0]],
            })
            print(f"  ✓ Facture {invoice_id} notifiée (fallback) à {partner_email}", flush=True)

        return True

    except Exception as e:
        print(f"  ✗ Erreur envoi facture {invoice_id} : {e}", flush=True)
        return False


def _get_partner_email(partner_id: int) -> str:
    try:
        rec = call("res.partner", "read", [[partner_id]], {"fields": ["email"]})[0]
        return (rec.get("email") or "").strip()
    except Exception:
        return ""


# ─── PIPELINE COMPLET ─────────────────────────────────────────────────────────
def auto_invoice_order(order_id: int, expected_total: float = None, send_email: bool = True) -> bool:
    """
    Pipeline complet pour une commande :
      1. Confirme la commande
      2. Crée la facture
      3. Valide la facture
      4. Envoie la facture par email (optionnel)

    À appeler depuis main.py après chaque create sale.order.

    Exemple :
        order_id = models.execute_kw(..., "sale.order", "create", [...])
        auto_invoice_order(order_id, expected_total=price_eur)
    """
    print(f"\n📄 Facturation commande {order_id}…", flush=True)

    if not confirm_order(order_id, expected_total):
        return False

    invoice_id = create_invoice(order_id)
    if not invoice_id:
        return False

    if not validate_invoice(invoice_id):
        return False

    # Envoi email désactivé — la facture reste dans Odoo, à envoyer manuellement
    # Pour réactiver : passer send_email=True dans auto_invoice_order()
    print(f"  ✅ Commande {order_id} → facture {invoice_id} validée (en attente d'envoi)\n", flush=True)
    return True


# ─── MODE RATTRAPAGE (lancer seul) ───────────────────────────────────────────
def catchup_unfactured_orders():
    """
    Traite toutes les commandes confirmées (state=sale) sans facture.
    Utile pour rattraper les commandes existantes.
    Lance : python billing.py
    """
    print("\n🔍 Recherche des commandes confirmées sans facture…", flush=True)

    # Commandes confirmées
    confirmed_ids = call(
        "sale.order", "search",
        [[("state", "=", "sale")]],
        {"order": "id asc"}
    )

    if not confirmed_ids:
        print("  ℹ Aucune commande confirmée trouvée.", flush=True)
        return

    # Filtrer celles qui ont déjà une facture
    orders_with_invoice = call(
        "account.move", "search_read",
        [[
            ("move_type", "=", "out_invoice"),
            ("state", "!=", "cancel"),
            ("invoice_origin", "!=", False)
        ]],
        {"fields": ["invoice_origin"]}
    )
    invoiced_origins = {r["invoice_origin"] for r in orders_with_invoice}

    # Noms des commandes confirmées
    orders = call(
        "sale.order", "read",
        [confirmed_ids],
        {"fields": ["id", "name", "partner_id", "amount_total", "origin"]}
    )

    to_process = [o for o in orders if o["name"] not in invoiced_origins]
    print(f"  → {len(to_process)} commande(s) à facturer sur {len(orders)} confirmées\n", flush=True)

    ok, ko = 0, 0
    for order in to_process:
        print(f"  Traitement : {order['name']} | {order['origin']} | {order['amount_total']:.2f} EUR", flush=True)
        success = auto_invoice_order(order["id"], send_email=False)
        if success:
            ok += 1
        else:
            ko += 1

    print(f"\n{'═'*50}", flush=True)
    print(f"  Résultat : {ok} facturées ✓   {ko} échecs ✗", flush=True)
    print(f"{'═'*50}\n", flush=True)


# ─── LANCEMENT STANDALONE ─────────────────────────────────────────────────────
if __name__ == "__main__":
    if not all([ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASSWORD]):
        print("❌ Variables d'environnement Odoo manquantes.")
        raise SystemExit(1)
    if not uid:
        print("❌ Authentification Odoo impossible.")
        raise SystemExit(1)

    catchup_unfactured_orders()
