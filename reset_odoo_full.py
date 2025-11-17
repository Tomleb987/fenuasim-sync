import os
import xmlrpc.client

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

print("🔌 Connexion Odoo…")

common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")


def safe_call(model, method, ids, msg):
    """Appelle une méthode Odoo en ignorant les erreurs."""
    if not ids:
        return
    try:
        models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, [ids])
        print(f"✅ {msg} ({model}.{method}, {len(ids)} enregistrements)")
    except Exception as e:
        print(f"⚠️ {msg} impossible ({model}.{method}) : {e}")


def wipe(model, domain=None):
    """Supprime un modèle en continuant même si Odoo bloque."""
    if domain is None:
        domain = []

    try:
        ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            model, 'search', [domain]
        )
        if ids:
            try:
                models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    model, 'unlink', [ids]
                )
                print(f"🗑 {model} : {len(ids)} supprimés.")
            except Exception as e:
                print(f"⚠️ Impossible de supprimer {model} (on continue) : {e}")
        else:
            print(f"ℹ️ {model} : aucun enregistrement.")
    except Exception as e:
        print(f"⚠️ Erreur lors de la recherche de {model} : {e}")


print("🔥 RESET COMPLET — version Odoo Online (avec annulation préalable)…")

# 1️⃣ COMMANDES CLIENT (sale.order)
#    - passer en annulé, puis supprimer
try:
    so_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'sale.order', 'search', [[]]
    )
    safe_call('sale.order', 'action_cancel', so_ids,
              "Annulation des commandes client")
    wipe('sale.order.line')
    wipe('sale.order')
except Exception as e:
    print(f"⚠️ Erreur traitement sale.order : {e}")

# 2️⃣ FACTURES / ÉCRITURES (account.move)
#    a) factures (move_type != entry)
try:
    inv_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'account.move', 'search', [[('move_type', '!=', 'entry')]]
    )
    safe_call('account.move', 'button_draft', inv_ids,
              "Passage des factures en brouillon")
    wipe('account.move', [('id', 'in', inv_ids)])
except Exception as e:
    print(f"⚠️ Erreur traitement factures : {e}")

#    b) écritures diverses (move_type = entry)
try:
    entry_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'account.move', 'search', [[('move_type', '=', 'entry')]]
    )
    safe_call('account.move', 'button_draft', entry_ids,
              "Passage des écritures diverses en brouillon")
    wipe('account.move', [('id', 'in', entry_ids)])
except Exception as e:
    print(f"⚠️ Erreur traitement écritures : {e}")

#    c) lignes comptables (devraient suivre les moves)
wipe('account.move.line')

# 3️⃣ PAIEMENTS (account.payment)
try:
    pay_ids = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'account.payment', 'search', [[]]
    )
    # selon versions : action_draft ou action_cancel
    safe_call('account.payment', 'action_draft', pay_ids,
              "Passage des paiements en brouillon")
    safe_call('account.payment', 'action_cancel', pay_ids,
              "Annulation des paiements")
    wipe('account.payment')
except Exception as e:
    print(f"⚠️ Erreur traitement paiements : {e}")

# 4️⃣ PRODUITS & CATEGORIES
wipe('product.product')
wipe('product.template')
wipe('product.category')

# 5️⃣ CLIENTS / PARTENAIRES (sauf ID 1 et utilisateur actif)
wipe('res.partner', [('id', 'not in', [1, 2])])

# 6️⃣ PIÈCES JOINTES
wipe('ir.attachment')

print("✅ RESET ODOO TERMINÉ — base normalement vidée au maximum.")
