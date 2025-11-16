from main import sync_airalo_orders, sync_stripe_payments

if __name__ == "__main__":
    print("🚀 Début synchronisation rapide Supabase → Odoo")

    sync_airalo_orders()
    sync_stripe_payments()

    print("✅ Synchronisation rapide terminée")
