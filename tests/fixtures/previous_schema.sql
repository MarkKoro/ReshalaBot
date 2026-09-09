CREATE TABLE IF NOT EXISTS users (
            tg_user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            accepted_offer_at TEXT,
            current_chat_id INTEGER DEFAULT 0,
            current_message_id INTEGER DEFAULT 0,
            current_message_kind TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_user_id INTEGER NOT NULL,
            payment_id TEXT UNIQUE NOT NULL,
            provider TEXT NOT NULL DEFAULT 'yookassa',
            plan_id TEXT NOT NULL,
            amount_rub INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            confirmation_url TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            raw_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            paid_at TEXT,
            finalized_at TEXT
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            tg_user_id INTEGER PRIMARY KEY,
            access_until TEXT NOT NULL,
            last_payment_id TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            renewal_notice_sent_at TEXT,
            expiration_notice_sent_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS invite_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_user_id INTEGER NOT NULL,
            invite_link TEXT NOT NULL,
            payment_id TEXT,
            expire_at TEXT,
            created_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS review_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS admin_states (
            tg_user_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
