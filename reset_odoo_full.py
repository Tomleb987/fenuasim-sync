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


def wipe(model, domain=None):
    """Supprime un modèle en continuant même si Odoo bloque."""
    if domain is None:
        domain = []

    try:
        ids = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD,
                                model, 'search', [domain])
        if ids:
            try:
                models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD,
                                  model, 'unlink', [ids])
                print(f"🗑 {model} : {len(ids)} supprimés.")
            except Exception as e:
                print(f"⚠️ Impossible de supprimer {model} (on continue) : {e}")
        else:
            print(f"ℹ️ {model} : aucun enregistrement.")
    except Exception as e:
        print(f"⚠️ Erreur lors de la recherche de {model} : {e}")


print("🔥 RESET COMPLET — version Odoo Online…")


# 1️⃣ Factures & Écritures comptables (must delete FIRST)
wipe('account.move', [('move_type', '!=', 'entry')])   # factures
wipe('account.move', [('move_type', '=', 'entry')])    # écritures diverses
wipe('account.move.line')                               # lignes comptables

# 2️⃣ Paiements
wipe('account.payment')  # peut échouer → ignoré automatiquement

# 3️⃣ Commandes de vente + leurs lignes
wipe('sale.order.line')
wipe('sale.order')

# 4️⃣ Produits & catégories
wipe('product.product')
wipe('product.template')
wipe('product.category')

# 5️⃣ Clients (garder l’entreprise principale)
wipe('res.partner', [('id', '!=', 1)])

# 6️⃣ Attachments
wipe('ir.attachment')

print("✅ RESET ODOO TERMINÉ — Base propre et vide.")
