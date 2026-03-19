# ============================================================
#  JDM CASH NOW – Sistema de Préstamos Multi-Rol (PostgreSQL)
#  Funciones:
#   - Pago de CAPITAL, INTERÉS o CUOTA (capital+interés)
#   - Si paga capital → reduce remaining automático
#   - Si se paga el TOTAL (capital + intereses) → préstamo se cierra (status='cerrado')
#   - Cobradores aislados (no ven clientes de otros)
#   - Admin reasigna clientes entre cobradores
#   - Admin/Supervisor puede mover un solo cliente de cobrador
#   - Compatible con Flask 3 (sin before_first_request)
#   - Tema Claro / Oscuro con botón de cambio
#   - Frecuencia: diario / semanal / quincenal / mensual
#   - Atrasos aproximados según frecuencia e intereses
#   - Registro de efectivo entregado por trabajador (Gastos de ruta)
#   - Enviar factura al cliente por  / SMS
# ============================================================
from __future__ import annotations

# ===============================
# IMPORTS OBLIGATORIOS
# ===============================
import os
import secrets
from datetime import datetime, date, timedelta
from urllib.parse import quote_plus

import pytz

from flask import (
    Flask,
    request,
    redirect,
    url_for,
    Response,
    render_template,
    render_template_string,
    session,
    flash,
    get_flashed_messages
)

from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

import psycopg2
import psycopg2.extras

# ===============================
# ZONA HORARIA OFICIAL DEL SISTEMA (RD DEFINITIVA)
# ===============================
UTC = pytz.utc
RD_TZ = pytz.timezone("America/Santo_Domingo")

def utc_now():
    """Hora actual en UTC (para guardar en DB)"""
    return datetime.utcnow()

def to_rd(dt):
    """Convierte una fecha UTC a hora RD"""
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = UTC.localize(dt)
    return dt.astimezone(RD_TZ)

# =============================
# CONFIGURACIÓN PRINCIPAL
# =============================
APP_BRAND = "JDM Cash Now"

# PIN administrativo
ADMIN_PIN = os.getenv("ADMIN_PIN", "5555")

# Roles disponibles
ROLES = ("admin", "supervisor", "cobrador")

# Moneda
CURRENCY = "RD$"

# WhatsApp SOS / recuperación
ADMIN_WHATSAPP = os.getenv("ADMIN_WHATSAPP", "3128565688")

# URL de la base de datos (Render)
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("❌ ERROR: Falta la DATABASE_URL en Render → Environment.")

# =============================
# CREAR APP FLASK
# =============================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(16))

# =============================
# CONFIGURACIÓN SUBIR FOTOS
# =============================
from werkzeug.utils import secure_filename
import time

UPLOAD_FOLDER = "static/uploads"

# crear carpeta automáticamente
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# guardar ruta en Flask
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


# =============================
# CONEXIÓN A BASE DE DATOS
# =============================
import psycopg2
import psycopg2.extras

def get_conn():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor
    )

# ============================================================
# 🔄 GENERAR CUOTAS ATRASADAS AUTOMÁTICO (CORREGIDO)
# ============================================================

def generar_atrasos():

    from psycopg2.extras import RealDictCursor

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        cur.execute("""
        SELECT id, amount, next_payment_date
        FROM loans
        WHERE next_payment_date < CURRENT_DATE
        """)
        vencidos = cur.fetchall()

        for v in vencidos:

            # evitar duplicar atraso
            cur.execute("""
            SELECT id FROM loan_arrears
            WHERE loan_id=%s AND due_date=%s AND paid=false
            """, (v["id"], v["next_payment_date"]))

            existe = cur.fetchone()

            if not existe:

                # guardar cuota atrasada
                cur.execute("""
                INSERT INTO loan_arrears (loan_id, due_date, amount)
                VALUES (%s,%s,%s)
                """, (v["id"], v["next_payment_date"], v["amount"]))

                # 🚨 NO mover fecha aquí
                # La fecha solo se mueve cuando el cliente paga

        conn.commit()

    finally:
        cur.close()
        conn.close()
# ======================================================
# 🧱 BLINDAJE BD – CÉDULA + FIRMA EN BASE DE DATOS
# (después de get_conn() y antes de @app.route)
# ======================================================
def ensure_legal_columns():
    conn = get_conn()
    cur = conn.cursor()
    try:
        # Si la tabla loans no existe aún, no tumbes la app
        cur.execute("""
            SELECT to_regclass('public.loans') AS t;
        """)
        row = cur.fetchone()
        if not row or not row.get("t"):
            # Todavía no existe loans (ej. primer deploy / init)
            return

        cur.execute("ALTER TABLE loans ADD COLUMN IF NOT EXISTS signature_b64 TEXT;")
        cur.execute("ALTER TABLE loans ADD COLUMN IF NOT EXISTS id_photo_b64 TEXT;")
        conn.commit()
    except Exception as e:
        # No tumbes el servidor por un blindaje; solo log
        print("⚠️ ensure_legal_columns() warning:", str(e))
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


# ✅ Ejecutar 1 vez al arrancar (está bien)
# ensure_legal_columns()

# 🔹 EJECUTAR AUTOMÁTICAMENTE AL INICIAR LA APP
# fix_cash_reports_schema()


# ============================================================
# 🏦 FIX BANCO – COLUMNAS FALTANTES EN cash_reports
# (SE EJECUTA SOLO UNA VEZ, NO BORRA NADA)
# ============================================================
def fix_cash_reports_schema():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        ALTER TABLE cash_reports
        ADD COLUMN IF NOT EXISTS client_id INTEGER,
        ADD COLUMN IF NOT EXISTS route_id INTEGER;
    """)

    conn.commit()
    cur.close()
    conn.close()


# 🔹 EJECUTAR AUTOMÁTICAMENTE AL INICIAR LA APP
fix_cash_reports_schema()



# =============================
# FORMATEO DE DINERO
# =============================

def fmt_money(val):
    try:
        v = float(val or 0)
    except (TypeError, ValueError):
        v = 0.0
    return f"{CURRENCY} {v:.2f}"


def get_theme():
    """Devuelve 'light' o 'dark' según lo guardado en sesión."""
    return session.get("theme", "light")


# =============================
# CREACIÓN / RESET DE TABLAS
# =============================

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    try:
        # ---- Tabla usuarios ----
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(100) UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role VARCHAR(20) NOT NULL,
                phone VARCHAR(50),              -- 📞 TELÉFONO DEL PRESTAMISTA
                created_at TIMESTAMP NOT NULL
            );
        """)

        # 🔒 BLINDAJE: por si la tabla ya existía SIN phone
        cur.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS phone VARCHAR(50);
        """)

        conn.commit()
        print("✔ Tabla users verificada (con teléfono)")
    finally:
        cur.close()
        conn.close()


        # ---- Tabla s ----
        cur.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id SERIAL PRIMARY KEY,
                first_name VARCHAR(100) NOT NULL,
                last_name VARCHAR(100),
                phone VARCHAR(50),
                address TEXT,
                document_id VARCHAR(100),
                route VARCHAR(100),
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """)

        # ---- Tabla préstamos ----
        cur.execute("""
            CREATE TABLE IF NOT EXISTS loans (
                id SERIAL PRIMARY KEY,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                amount NUMERIC(12,2) NOT NULL,
                rate NUMERIC(5,2) NOT NULL DEFAULT 0,
                frequency VARCHAR(20) NOT NULL,
                start_date DATE NOT NULL,
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                remaining NUMERIC(12,2),
                total_interest_paid NUMERIC(12,2),
                status VARCHAR(20),
                term_count INTEGER,
                end_date DATE
            );
        """)

        # ---- Tabla pagos ----
        cur.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                loan_id INTEGER NOT NULL REFERENCES loans(id) ON DELETE CASCADE,
                amount NUMERIC(12,2) NOT NULL,
                type VARCHAR(20),
                note TEXT,
                date DATE NOT NULL,
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL
            );
        """)

        # ---- Tabla auditoría ----
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                action VARCHAR(255) NOT NULL,
                detail TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """)

        # ---- Tabla efectivo entregado (Gastos de ruta) ----
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cash_reports (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                date DATE NOT NULL,
                amount NUMERIC(12,2) NOT NULL,
                note TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """)

        # ✅ Asegurar columnas nuevas en loans (para BD viejas)
        cur.execute("""
            ALTER TABLE loans
            ADD COLUMN IF NOT EXISTS remaining NUMERIC(12,2);
        """)
        cur.execute("""
            ALTER TABLE loans
            ADD COLUMN IF NOT EXISTS total_interest_paid NUMERIC(12,2) DEFAULT 0;
        """)
        cur.execute("""
            ALTER TABLE loans
            ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'activo';
        """)
        cur.execute("""
            ALTER TABLE loans
            ADD COLUMN IF NOT EXISTS term_count INTEGER;
        """)
        cur.execute("""
            ALTER TABLE loans
            ADD COLUMN IF NOT EXISTS end_date DATE;
        """)

        # ✅ Asegurar columna ruta en s
        cur.execute("""
            ALTER TABLE clients
            ADD COLUMN IF NOT EXISTS route VARCHAR(100);
        """)

        # ✅ Inicializar valores nulos si la tabla ya existía
        cur.execute("""
            UPDATE loans
            SET remaining = amount
            WHERE remaining IS NULL;
        """)
        cur.execute("""
            UPDATE loans
            SET total_interest_paid = 0
            WHERE total_interest_paid IS NULL;
        """)
        cur.execute("""
            UPDATE loans
            SET status = 'activo'
            WHERE status IS NULL OR status = '';
        """)

        # Crear admin si no existe
        cur.execute("SELECT COUNT(*) AS c FROM users;")
        if cur.fetchone()["c"] == 0:
            cur.execute("""
                INSERT INTO users (username, password_hash, role, created_at)
                VALUES (%s, %s, %s, %s)
            """, (
                "admin",
                generate_password_hash("admin"),
                "admin",
                datetime.utcnow(),
            ))
            print("✔ Usuario admin creado (user=admin, pass=admin)")

        conn.commit()
        print("✔ Base de datos inicializada correctamente")
    cur.close()
    conn.close()


# ============================================================
#  PARTE 2 — Usuario actual, roles, auditoría, layout y login
# ============================================================

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = %s;", (uid,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            flash("Debe iniciar sesión primero.", "warning")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def role_required(*allowed_roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user or user["role"] not in allowed_roles:
                flash("No tiene permiso para acceder aquí.", "danger")
                return redirect(url_for("index"))
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def admin_required(fn):
    return role_required("admin")(fn)


def log_action(user_id, action, detail=""):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO audit_log (user_id, action, detail)
        VALUES (%s, %s, %s)
    """, (user_id, action, detail))
    conn.commit()
    cur.close()
    conn.close()


BASE_STYLE = """
<style>
*,
*::before,
*::after {
  box-sizing: border-box;
}

html, body {
  width: 100%;
  max-width: 100%;
  overflow-x: hidden;
  margin: 0;
  padding: 0;
}

:root {
  --green-50: #ecfdf3;
  --green-100: #dcfce7;
  --green-200: #bbf7d0;
  --green-600: #16a34a;
  --green-700: #15803d;
  --green-800: #166534;
  --red-600: #dc2626;
  --slate-800: #0f172a;
  --slate-900: #020617;
}
img {
  max-width: 100%;
  height: auto;
  display: block;
}
button,
.btn,
input,
select,
textarea {
  max-width: 100%;
}

body {
  margin: 0;
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

/* =============================
   TEMA CLARO / OSCURO
============================= */
body.theme-light {
  background: var(--green-50);
  color: #022c22;
}

body.theme-dark {
  background: linear-gradient(135deg, #06131a 0%, #022c22 45%, #111827 100%);
  color: #f9fafb;
}

/* =============================
   TOP BAR
============================= */
header.topbar {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 14px 18px;

  background: linear-gradient(135deg, #166534, #22c55e);
  color: #ffffff;

  border-bottom: none;
  box-shadow: 0 10px 28px rgba(0,0,0,.25);
}
.topbar-title {
  font-weight: 900;
  font-size: 18px;
  color: #ffffff;
}
body.theme-dark header.topbar {
  background: linear-gradient(135deg, #064e3b, #16a34a);
}


body.theme-dark header.topbar {
  background: #022c22;
  border-bottom-color: #064e3b;
}

/* =============================
   CONTENEDOR
============================= */
.container {
  width: 100%;
  max-width: 100%;
  padding: 12px;
  overflow-x: hidden;
}

/* =============================
   CARDS
============================= */
.card {
  width: 100%;
  max-width: 100%;
  padding: 14px;
  border-radius: 16px;
  overflow: hidden;
}

body.theme-dark .card {
  background: rgba(15,23,42,.96);
}

/* =============================
   TABLAS
============================= */
table {
  width: 100%;
  border-collapse: collapse;
  font-size: .9rem;
}

th, td {
  padding: 9px 10px;
  border-bottom: 1px solid rgba(148,163,184,.4);
}

th {
  background: #ecfdf3;
  text-align: left;
}

body.theme-dark th {
  background: rgba(30,64,175,.25);
}

/* =============================
   BOTONES
============================= */
.btn {
  padding: 8px 16px;
  border-radius: 999px;
  border: none;
  cursor: pointer;
  font-size: .9rem;
  font-weight: 700;
}

.btn-primary {
  background: var(--green-600);
  color: white;
}

.btn-secondary {
  background: #e5e7eb;
  color: #0f172a;
}

body.theme-dark .btn-secondary {
  background: #334155;
  color: #e5e7eb;
}

  body {
    overflow-x: visible !important;
  }

/* =============================
   BADGES
============================= */
.badge {
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
}

.badge-green { background:#16a34a; color:white; }
.badge-red   { background:#dc2626; color:white; }

/* =============================
   iOS GLASS NAVIGATION
============================= */
nav.main-nav {
  display: flex;
  flex-wrap: nowrap;
  gap: 10px;
  overflow-x: auto;
  justify-content: flex-start;
  padding: 8px 6px;
  -webkit-overflow-scrolling: touch;
}

nav.main-nav::-webkit-scrollbar {
  display: none;
}

nav.main-nav a {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border-radius: 999px;
  font-weight: 700;
  font-size: 14px;
  text-decoration: none;
  white-space: nowrap;
  flex-shrink: 0;

  background: rgba(255,255,255,.6);
  backdrop-filter: blur(18px) saturate(160%);
  -webkit-backdrop-filter: blur(18px) saturate(160%);
  border: 1px solid rgba(255,255,255,.35);
  box-shadow: 0 6px 16px rgba(0,0,0,.15),
              inset 0 1px 0 rgba(255,255,255,.5);
  color: #0f172a;
}

nav.main-nav a:hover {
  background: rgba(255,255,255,.85);
}

body.theme-dark nav.main-nav a {
  background: rgba(15,23,42,.55);
  color: #e5e7eb;
  border: 1px solid rgba(255,255,255,.12);
}

/* =============================
   FORMULARIOS MOBILE (CREAR )
============================= */
@media (max-width: 768px) {

  form {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  form label {
    font-size: .85rem;
    font-weight: 600;
  }

  form input,
  form select,
  form textarea,
  form button {
    width: 100%;
    font-size: 15px;
    padding: 10px 14px;
    border-radius: 14px;
  }

  header.topbar {
    flex-direction: column;
    gap: 10px;
  }

  .container {
    padding: 10px;
  }

  .card {
    padding: 14px;
    border-radius: 16px;
  }

  table {
    font-size: .8rem;
  }
  /* ===============================
   📱 SCROLL LATERAL EN TABLAS
=============================== */
.table-scroll {
  width: 100%;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
}

.table-scroll table {
  min-width: 720px;
}

}
</style>
"""


# =============================
# LAYOUT GENERAL (iOS GLASS + MENU iPHONE SWIPE LEFT)
# =============================

TPL_LAYOUT = """
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>{{ app_brand }}</title>
  <meta name="viewport"
content="width=device-width, initial-scale=1.0, maximum-scale=5.0, user-scalable=yes">


  <!-- 🔹 PWA -->
  <link rel="manifest" href="/static/manifest.json">
  <meta name="theme-color" content="#16a34a">

  """ + BASE_STYLE + """

<style>

/* ===============================
   🍏 TOPBAR iOS REAL
=============================== */
header.topbar{
  position:relative;
  display:flex;
  align-items:center;
  justify-content:center;
  padding:14px;
}

/* ===============================
   💎 BOTÓN PREMIUM (GLASS + INICIO)
=============================== */
.premium-btn{
  position:absolute;
  left:14px;
  top:50%;
  transform:translateY(-50%);

  display:flex;
  flex-direction:column;
  align-items:center;
  justify-content:center;
  gap:2px;

  width:56px;
  height:56px;

  border-radius:18px;
  border:none;
  cursor:pointer;

  background:rgba(255,255,255,0.78);
  color:#0f172a;

  backdrop-filter: blur(22px) saturate(180%);
  -webkit-backdrop-filter: blur(22px) saturate(180%);

  box-shadow:
    0 10px 28px rgba(0,0,0,0.28),
    inset 0 1px 0 rgba(255,255,255,0.75);

  transition:
    box-shadow 0.2s ease,
    background 0.2s ease;
}

.premium-btn span.icon{
  font-size:18px;
  line-height:1;
}

.premium-btn span.text{
  font-size:11px;
  font-weight:700;
  line-height:1;
}

/* 🌙 DARK MODE */
body.theme-dark .premium-btn{
  background:rgba(15,23,42,0.65);
  color:#e5e7eb;
  box-shadow:
    0 10px 28px rgba(0,0,0,0.5),
    inset 0 1px 0 rgba(255,255,255,0.12);
}

/* ===============================
   ✨ ANIMACIÓN BOUNCE iOS
=============================== */
.premium-btn.bounce{
  animation:iosBounce 0.38s cubic-bezier(.34,1.56,.64,1);
}

@keyframes iosBounce{
  0%   { transform:translateY(-50%) scale(1); }
  35%  { transform:translateY(-50%) scale(0.9); }
  65%  { transform:translateY(-50%) scale(1.08); }
  100% { transform:translateY(-50%) scale(1); }
}

/* ===============================
   📱 TÍTULO CENTRADO
=============================== */
.topbar-title{
  font-weight:800;
  text-align:center;
  pointer-events:none;
}

/* ===============================
   ☰ MENU LATERAL iOS (LEFT)
=============================== */
.side-menu{
  position:fixed;
  top:0;
  left:0;
  width:260px;
  height:100%;
  padding:18px;
  background:linear-gradient(180deg,#0f172a,#020617);
  backdrop-filter: blur(20px) saturate(160%);
  transform:translateX(-100%);
  transition:transform 0.35s cubic-bezier(.4,0,.2,1);
  z-index:9999;
}

.side-menu.open{
  transform:translateX(0);
}

.side-menu a{
  display:block;
  padding:12px;
  margin-bottom:6px;
  border-radius:14px;
  color:#e5e7eb;
  text-decoration:none;
  font-weight:600;
}

.side-menu a:hover{
  background:rgba(255,255,255,0.12);
}

.menu-user{
  margin-bottom:16px;
  padding-bottom:12px;
  border-bottom:1px solid rgba(255,255,255,0.15);
  color:#ecfdf3;
}

/* ===============================
   📱 OVERLAY
=============================== */
.menu-overlay{
  position:fixed;
  inset:0;
  background:rgba(0,0,0,0.35);
  backdrop-filter: blur(2px);
  display:none;
  z-index:9998;
}

/* ===============================
   📱 FIX SCROLL iPHONE + ANDROID
=============================== */
@media (max-width: 768px) {
  html, body {
    width: 100%;
    overflow-x: auto !important;
    overflow-y: auto !important;
    -webkit-overflow-scrolling: touch;
    overscroll-behavior-x: auto;
  }

  /* Evita que el menú capture el touch */
  .side-menu,
  .menu-overlay {
    touch-action: pan-y pan-x;
  }
}

.menu-overlay.show{
  display:block;
}
</style>
</head>

<body class="theme-{{ theme or 'light' }} {{ page_class or '' }}">

<header class="topbar">
  {% if user %}
    <button class="premium-btn" onclick="toggleMenuWithHaptic()">
      <span class="icon">☰</span>
      <span class="text">Inicio</span>
    </button>
  {% endif %}

  <div class="topbar-title">
     {{ app_brand }}
  </div>
</header>

{% if user %}
<div id="menuOverlay" class="menu-overlay" onclick="closeMenu()"></div>

<div id="sideMenu" class="side-menu">

  <div class="menu-user">
    👤 {{ user.username }}<br>
    <small>{{ user.role }}</small>
  </div>

  <a href="{{ url_for('index') }}">🏠 Inicio</a>
  <a href="{{ url_for('clients') }}">👥 Clientes</a>
  <a href="{{ url_for('loans') }}">💳 Préstamos</a>
  <a href="{{ url_for('bank_home') }}">🏦 Banco</a>

  {% if user.role in ['admin','supervisor'] %}
    <a href="{{ url_for('reportes') }}">📊 Reportes</a>
    <a href="{{ url_for('audit') }}">🧾 Auditoría</a>
    <a href="{{ url_for('users') }}">👤 Usuarios</a>
    <a href="{{ url_for('reassign_clients') }}">🛣️ Rutas</a>
  {% endif %}

  <a href="{{ url_for('toggle_theme') }}">
    {% if theme == 'dark' %}🌙 Oscuro{% else %}☀️ Claro{% endif %}
  </a>

  <a href="{{ url_for('logout') }}">🚪 Salir</a>

</div>
{% endif %}

<div class="container">
  {{ body|safe }}
</div>

<!-- ===============================
   🍏 HAPTIC + BOUNCE + SWIPE
=============================== -->
<script>
let menuOpen = false;
let startX = 0;

function toggleMenuWithHaptic(){
  const btn = document.querySelector(".premium-btn");

  // Bounce
  btn.classList.remove("bounce");
  void btn.offsetWidth;
  btn.classList.add("bounce");

  // Haptic
  if (navigator.vibrate) {
    navigator.vibrate(12);
  }

  toggleMenu();
}

function toggleMenu(){
  menuOpen ? closeMenu() : openMenu();
}

function openMenu(){
  document.getElementById("sideMenu").classList.add("open");
  document.getElementById("menuOverlay").classList.add("show");
  menuOpen = true;
}

function closeMenu(){
  document.getElementById("sideMenu").classList.remove("open");
  document.getElementById("menuOverlay").classList.remove("show");
  menuOpen = false;
}

// Swipe iOS
document.addEventListener("touchstart", e=>{
  startX = e.touches[0].clientX;
});

document.addEventListener("touchend", e=>{
  let endX = e.changedTouches[0].clientX;

  if(startX < 30 && endX - startX > 80){
    openMenu();
  }
  if(menuOpen && startX - endX > 80){
    closeMenu();
  }
});
</script>

</body>
</html>
"""


# ============================================================
# 🔒 BLINDAJE USERS: ASEGURAR COLUMNA phone
# (SE EJECUTA SIEMPRE AL INICIAR LA APP)
# ============================================================
def ensure_users_phone_column():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS phone VARCHAR(50);
        """)
        conn.commit()
        print("✔ Columna phone verificada en users")
    except Exception as e:
        conn.rollback()
        print("⚠ Error asegurando columna phone:", e)
    finally:
        cur.close()
        conn.close()


# 🔥 EJECUTAR AL ARRANCAR LA APP
ensure_users_phone_column()




# =============================
# LOGIN
# =============================

TPL_LOGIN = """
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8"/>
<title>{{ app_brand }} - Login</title>
<meta name="viewport" content="width=device-width, initial-scale=1">

<style>
body {
    background:#e8f5e9;
    font-family: system-ui;
    margin:0;
    padding:0;
}
.header {
    background:#c8e6c9;
    padding:15px;
    text-align:center;
    font-size:22px;
    font-weight:700;
    color:#1b5e20;
}
.card {
    background:white;
    width:90%;
    max-width:400px;
    margin:40px auto;
    padding:25px;
    border-radius:15px;
    box-shadow:0 4px 10px rgba(0,0,0,0.15);
}
h1 {
    font-size:32px;
    text-align:center;
    background: linear-gradient(90deg, #b91c1c, #4b0082);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    font-weight:900;
    margin-bottom:20px;
}
label {
    font-size:15px;
    color:#1b5e20;
    font-weight:600;
}
input {
    width:100%;
    padding:10px;
    margin-top:5px;
    margin-bottom:12px;
    border-radius:8px;
    border:1px solid #a5d6a7;
    font-size:16px;
}
button {
    width:100%;
    padding:12px;
    border:none;
    background:#2e7d32;
    color:white;
    font-size:18px;
    font-weight:700;
    border-radius:10px;
    cursor:pointer;
}
.flash-danger {
    background:#ffcdd2;
    padding:10px;
    border-radius:10px;
    color:#b71c1c;
    margin-bottom:15px;
    text-align:center;
    font-weight:600;
}
</style>

</head>
<body>

<div class="header">JDM Cash Now</div>

<div class="card">
    {% if flashes %}
        {% for cat, msg in flashes %}
            <div class="flash-{{ cat }}">{{ msg }}</div>
        {% endfor %}
    {% endif %}

    <h1 style="color:#D4AF37; text-align:center; font-weight:800;">
    JDM Cash Now
</h1>


    <form method="post">
        <label>Usuario</label>
        <input name="username" required>

        <label>Contraseña</label>
        <input type="password" name="password" required>

        <button>Entrar</button>
    </form>

    <p style="margin-top:15px;text-align:center;font-size:14px;">
        <a href="{{ url_for('forgot_password') }}" style="color:#2e7d32;text-decoration:none;">
            ¿Olvidó su contraseña?
        </a>
    </p>

    <p style="margin-top:5px;text-align:center;font-size:14px;">
        <a href="https://wa.me/{{ admin_whatsapp }}" target="_blank" style="color:#1b5e20;">
            Recuperar por WhatsApp ({{ admin_whatsapp }})
        </a>
    </p>
</div>

</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE username = %s;", (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()

        # ===============================
        # VALIDAR USUARIO
        # ===============================
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Usuario o contraseña incorrectos.", "danger")
            return render_template_string(
                TPL_LOGIN,
                flashes=get_flashed_messages(with_categories=True),
                app_brand=APP_BRAND,
                admin_whatsapp=ADMIN_WHATSAPP
            )

        # ===============================
        # INICIAR SESIÓN LIMPIA
        # ===============================
        session.clear()

        session["user_id"] = user["id"]
        session["role"] = user.get("role")

        # ===============================
        # 🔥 DATOS DEL COBRADOR (CRÍTICO)
        # ===============================
        session["collector_name"] = user.get("name")
        session["collector_phone"] = user.get("phone")

        # ===============================
        # AUDITORÍA
        # ===============================
        log_action(user["id"], "login", "Inicio de sesión")

        flash(f"Bienvenido, {user['username']}", "success")
        return redirect(url_for("index"))

    return render_template_string(
        TPL_LOGIN,
        flashes=get_flashed_messages(with_categories=True),
        app_brand=APP_BRAND,
        admin_whatsapp=ADMIN_WHATSAPP
    )



# ============================================================
#  BOTÓN CAMBIAR TEMA
# ============================================================
@app.route("/toggle-theme")
@login_required
def toggle_theme():
    current = session.get("theme", "light")
    session["theme"] = "dark" if current == "light" else "light"
    ref = request.referrer or url_for("index")
    return redirect(ref)


# ============================================================
#  PWA MANIFEST (EVITA 404 EN LOGS)
# ============================================================
@app.route("/static/manifest.json")
def manifest():
    return "", 204



# ============================================================
# 📊 DASHBOARD — UNIFORME / MISMO TAMAÑO / BORDES IGUALES
# ============================================================
@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()



    if user["role"] not in ("admin", "supervisor", "cobrador"):
        flash("Acceso no permitido", "danger")
        return redirect(url_for("login"))

    conn = get_conn()
    cur = conn.cursor()

    user_filter_sql = ""
    pay_filter_sql = ""

    if user["role"] == "cobrador":
        user_filter_sql = f"AND created_by = {user['id']}"
        pay_filter_sql = f"WHERE created_by = {user['id']}"

    try:
        query = f"""
        SELECT
            (SELECT COUNT(*) FROM clients WHERE 1=1 {user_filter_sql}) AS total_clients,

            (SELECT COUNT(*) FROM loans WHERE UPPER(status)='ACTIVO' {user_filter_sql}) AS active_loans,

            (
                SELECT COALESCE(SUM(
                    l.remaining - (COALESCE(l.total_interest,0) - COALESCE(l.total_interest_paid,0))
                ),0)
                FROM loans l
                WHERE UPPER(l.status)='ACTIVO' {user_filter_sql}
            ) AS capital,

            (
                SELECT COALESCE(SUM(remaining),0)
                FROM loans
                WHERE UPPER(status)='ACTIVO' {user_filter_sql}
            ) AS total_en_calle,

            (
                SELECT COALESCE(SUM(interest),0)
                FROM payments
                WHERE COALESCE(status,'') <> 'ANULADO'
                AND EXTRACT(YEAR FROM date) = EXTRACT(YEAR FROM CURRENT_DATE)
                {f"AND created_by = {user['id']}" if user["role"]=="cobrador" else ""}
            ) AS interes_pagado,
			
            (
                SELECT COALESCE(SUM(amount),0)
                FROM payments
                WHERE DATE(date)=CURRENT_DATE
                AND status <> 'ANULADO'
                {f"AND created_by = {user['id']}" if user["role"]=="cobrador" else ""}
            ) AS cobrado_hoy,

            (
                SELECT COALESCE(SUM(interest),0)
                FROM payments
                WHERE DATE(date)=CURRENT_DATE
                AND status <> 'ANULADO'
                {f"AND created_by = {user['id']}" if user["role"]=="cobrador" else ""}
            ) AS interes_hoy,

            (
                SELECT COUNT(*)
                FROM loans
                WHERE UPPER(status)='ATRASADO'
                {user_filter_sql}
            ) AS prestamos_atrasados,

            (
                SELECT COALESCE(SUM(amount),0)
                FROM payments
                WHERE date >= CURRENT_DATE - INTERVAL '7 days'
                {f"AND created_by = {user['id']}" if user["role"]=="cobrador" else ""}
            ) AS kpi_semanal,

            (
                SELECT COALESCE(SUM(amount),0)
                FROM payments
                WHERE status <> 'ANULADO'
                AND DATE_TRUNC('month', date)=DATE_TRUNC('month', CURRENT_DATE)
                {f"AND created_by = {user['id']}" if user["role"]=="cobrador" else ""}
            ) AS kpi_mensual,

            (
                SELECT COALESCE(SUM(amount),0)
                FROM payments
                WHERE COALESCE(status,'') <> 'ANULADO'
                AND EXTRACT(YEAR FROM date) = EXTRACT(YEAR FROM CURRENT_DATE)
                {f"AND created_by = {user['id']}" if user["role"]=="cobrador" else ""}
            ) AS kpi_anual
        """
		
        cur.execute(query)
        row = cur.fetchone()

        total_clients = row["total_clients"] or 0
        active_loans = row["active_loans"] or 0
        capital_prestado = float(row["capital"] or 0)
        total_en_calle = float(row["total_en_calle"] or 0)
        interes_pagado = float(row["interes_pagado"] or 0)
        kpi_semanal = float(row["kpi_semanal"] or 0)
        kpi_mensual = float(row["kpi_mensual"] or 0)
        kpi_anual = float(row["kpi_anual"] or 0)

        cobrado_hoy = float(row["cobrado_hoy"] or 0)
        interes_hoy = float(row["interes_hoy"] or 0)
        prestamos_atrasados = row["prestamos_atrasados"] or 0

        total_empleados = 0
        if user["role"] in ("admin", "supervisor"):
            cur.execute("SELECT COUNT(*) AS total FROM users WHERE role='cobrador'")
            total_empleados = cur.fetchone()["total"] or 0

    finally:
        cur.close()
        conn.close()

    empleados_html = ""
    if user["role"] != "cobrador":
        empleados_html = f"""
        <a href="/users" class="kpi-link">
          <div class="kpi-card kpi-blue">
            <div class="kpi-icon">👥</div>
            <div class="kpi-title">Empleados</div>
            <div class="value">{total_empleados}</div>
          </div>
        </a>
        """

    body = f"""
<style>

.dashboard-header {{
  background: linear-gradient(135deg,#1b5e20,#43a047);
  color:white;
  padding:26px 20px;
  border-radius:28px;
  text-align:center;
  box-shadow:0 18px 40px rgba(0,0,0,.25);
}}

.kpi-grid {{
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:16px;
  margin-top:20px;
}}

.kpi-link {{
  text-decoration:none;
  color:inherit;
  display:block;
}}

.kpi-card {{
  border-radius:22px;
  width:100%;
  height:150px;
  padding:18px;
  color:white;
  text-align:center;
  font-weight:700;

  display:flex;
  flex-direction:column;
  justify-content:center;
  align-items:center;

  box-shadow:0 8px 20px rgba(0,0,0,.15);
  transition:.12s;
}}

.kpi-link:active .kpi-card {{
  transform:scale(.96);
  opacity:.9;
}}

.kpi-blue {{ background:#4a90e2; }}
.kpi-green {{ background:#43a047; }}
.kpi-purple {{ background:#5c6bc0; }}
.kpi-red {{ background:#e57373; }}
.kpi-yellow {{ background:#f5c542; color:#222; }}
.kpi-orange {{ background:#ff9800; }}
.kpi-darkgreen {{ background:#4caf50; }}
.kpi-deeppurple {{ background:#673ab7; }}

.kpi-icon {{
  width:44px;
  height:44px;
  border-radius:50%;
  background:rgba(255,255,255,.25);
  display:flex;
  align-items:center;
  justify-content:center;
  font-size:22px;
  margin-bottom:8px;
}}

.kpi-title {{
  font-size:15px;
  font-weight:800;
}}

.value {{
  font-size:22px;
  font-weight:900;
  margin-top:6px;
}}

</style>

<div class="dashboard-header">
<h1>💵 JDM Cash Now 💵</h1>
<small>Panel principal</small>
</div>

<div class="kpi-grid">

<a href="/reportes" class="kpi-link">
<div class="kpi-card" style="background:#16a34a">
<div class="kpi-icon">💰</div>
<div class="kpi-title">Cobrado Hoy</div>
<div class="value">{fmt_money(cobrado_hoy)}</div>
</div>
</a>

<a href="/reportes" class="kpi-link">
<div class="kpi-card" style="background:#22c55e">
<div class="kpi-icon">🔥</div>
<div class="kpi-title">Interés Hoy</div>
<div class="value">{fmt_money(interes_hoy)}</div>
</div>
</a>

<a href="/loans" class="kpi-link">
<div class="kpi-card" style="background:#ef4444">
<div class="kpi-icon">⚠️</div>
<div class="kpi-title">Atrasados</div>
<div class="value">{prestamos_atrasados}</div>
</div>
</a>

<a href="/loans" class="kpi-link">
<div class="kpi-card kpi-blue">
<div class="kpi-icon">📄</div>
<div class="kpi-title">Préstamos</div>
<div class="value">{active_loans}</div>
</div>
</a>

<a href="/clients" class="kpi-link">
<div class="kpi-card kpi-green">
<div class="kpi-icon">👤</div>
<div class="kpi-title">Clientes</div>
<div class="value">{total_clients}</div>
</div>
</a>

<a href="/loans" class="kpi-link">
<div class="kpi-card kpi-purple">
<div class="kpi-icon">💼</div>
<div class="kpi-title">Capital pendiente</div>
<div class="value">{fmt_money(capital_prestado)}</div>
</div>
</a>

<a href="/loans" class="kpi-link">
<div class="kpi-card kpi-red">
<div class="kpi-icon">🔥</div>
<div class="kpi-title">En la calle</div>
<div class="value">{fmt_money(total_en_calle)}</div>
</div>
</a>

<a href="/reportes" class="kpi-link">
<div class="kpi-card kpi-green">
<div class="kpi-icon">💰</div>
<div class="kpi-title">Interés</div>
<div class="value">{fmt_money(interes_pagado)}</div>
</div>
</a>

<a href="/reportes" class="kpi-link">
<div class="kpi-card kpi-blue">
<div class="kpi-icon">📊</div>
<div class="kpi-title">Semana</div>
<div class="value">{fmt_money(kpi_semanal)}</div>
</div>
</a>

<a href="/reportes" class="kpi-link">
<div class="kpi-card kpi-yellow">
<div class="kpi-icon">📆</div>
<div class="kpi-title">Mes</div>
<div class="value">{fmt_money(kpi_mensual)}</div>
</div>
</a>

<a href="/reportes" class="kpi-link">
<div class="kpi-card kpi-blue">
<div class="kpi-icon">📆</div>
<div class="kpi-title">Anual</div>
<div class="value">{fmt_money(kpi_anual)}</div>
</div>
</a>

<a href="/bank/cobro-sabado" class="kpi-link">
<div class="kpi-card kpi-darkgreen">
<div class="kpi-icon">💰</div>
<div class="kpi-title">Cobro Sábado</div>
</div>
</a>

<a href="/bank/ranking" class="kpi-link">
<div class="kpi-card kpi-orange">
<div class="kpi-icon">🏆</div>
<div class="kpi-title">Ranking Morosos</div>
</div>
</a>

<a href="/bank/resumen" class="kpi-link">
<div class="kpi-card kpi-orange">
<div class="kpi-icon">💰</div>
<div class="kpi-title">Resumen Financiero</div>
</div>
</a>

<a href="/bank/credit-history" class="kpi-link">
<div class="kpi-card kpi-darkgreen">
<div class="kpi-icon">💳</div>
<div class="kpi-title">Historial de Crédito</div>
</div>
</a>

<a href="/bank/cierre-semanal" class="kpi-link">
<div class="kpi-card" style="background:linear-gradient(135deg,#16a34a,#22c55e);">
<div class="kpi-icon">🧾</div>
<div class="kpi-title">Cierre Semanal</div>
</div>
</a>

<a href="/bank/agregar-dinero" class="kpi-link">
<div class="kpi-card" style="background:#dc2626;color:white">
<div class="kpi-icon">💰</div>
<div class="kpi-title">Agregar Dinero Banco</div>
</div>
</a>

<a href="/bank/historial-cierres" class="kpi-link">
<div class="kpi-card" style="background:#ff7a00;color:white;">
<div class="kpi-icon">📚</div>
<div class="kpi-title">Cuadres Cerrados</div>
<div class="value">Historial</div>
</div>
</a>

<a href="/bank/historial-depositos" class="kpi-link">
<div class="kpi-card" style="background:#2563eb;color:white">
<div class="kpi-icon">🏦</div>
<div class="kpi-title">Historial Depósitos</div>
<div class="value">Admin</div>
</div>
</a>

<a href="/bank/check-client" class="kpi-link">
<div class="kpi-card kpi-deeppurple">
<div class="kpi-icon">🔎</div>
<div class="kpi-title">Consultar por Cédula</div>
</div>
</a>

<div class="kpi-card" onclick="location.href='/prestamos/pagados'" style="background:#27ae60; cursor:pointer">
<div style="font-size:30px">📄</div>
<div>Préstamos Pagados</div>
</div>

{empleados_html}

</div>
"""

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        app_brand=APP_BRAND,
        flashes=get_flashed_messages(with_categories=True),
        theme=get_theme()
    )





# ============================================================
#  USUARIOS
# ============================================================

@app.route("/users")
@login_required
@admin_required
def users():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, username, role, created_at
        FROM users
        ORDER BY id DESC
    """)
    rows = cur.fetchall() or []

    cur.close()
    conn.close()

    users_html = "".join([
        f"""
        <tr>
          <td>{u['id']}</td>
          <td>{u['username']}</td>
          <td>{u['role']}</td>
          <td>{u['created_at']}</td>
          <td>
            <form action="{url_for('delete_user', user_id=u['id'])}"
                  method="post"
                  onsubmit="return confirm('¿Eliminar usuario permanentemente?');"
                  style="display:inline;">
              <input name="pin" placeholder="PIN" required style="width:80px;">
              <button class="btn btn-danger">Eliminar</button>
            </form>
          </td>
        </tr>
        """
        for u in rows
    ])

    if not users_html:
        users_html = "<tr><td colspan='5'>No hay usuarios</td></tr>"

    body = f"""
    <div class="card">
      <h2>👤 Usuarios</h2>

      <a href="{url_for('new_user')}" class="btn btn-primary" style="margin-bottom:12px;">
        ➕ Nuevo usuario
      </a>

      <div class="table-wrapper">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Usuario</th>
              <th>Rol</th>
              <th>Creado</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {users_html}
          </tbody>
        </table>
      </div>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=current_user(),
        app_brand=APP_BRAND,
        admin_whatsapp=ADMIN_WHATSAPP,
        flashes=get_flashed_messages(with_categories=True),
        theme=get_theme()
    )
# ============================================================
# 👥 EMPLEADOS / COBRADORES (SEGURO SIN FALLA)
# ============================================================
@app.route("/employees")
@login_required
def employees():
    user = current_user()

    if user["role"] == "cobrador":
        flash("Acceso no autorizado.", "danger")
        return redirect(url_for("index"))

    conn = get_conn()
    cur = conn.cursor()

    # --------------------------------------------------------
    # Intentar con columna phone (si existe)
    # --------------------------------------------------------
    try:
        cur.execute("""
            SELECT id, username, role, COALESCE(phone,'') AS phone
            FROM users
            WHERE role IN ('cobrador','supervisor','admin')
            ORDER BY role, username
        """)
        rows = cur.fetchall() or []

    # --------------------------------------------------------
    # Si NO existe phone → fallback seguro
    # --------------------------------------------------------
    except Exception:
        conn.rollback()
        cur.execute("""
            SELECT id, username, role
            FROM users
            WHERE role IN ('cobrador','supervisor','admin')
            ORDER BY role, username
        """)
        rows = cur.fetchall() or []
        # inyectar phone vacío
        rows = [{**u, "phone": ""} for u in rows]

    finally:
        cur.close()
        conn.close()

    # --------------------------------------------------------
    # Render tarjetas
    # --------------------------------------------------------
    cards_html = "".join([
        f"""
        <div class="employee-glass-card">
          <div class="employee-avatar">👤</div>

          <div class="employee-info">
            <div class="employee-name">{u['username']}</div>
            <div class="employee-phone">
              📞 {u.get('phone') or 'Sin teléfono'}
            </div>

            <span class="employee-badge badge-{u['role']}">
              {u['role'].capitalize()}
            </span>
          </div>
        </div>
        """
        for u in rows
    ])

    body = f"""
    <style>
    .employee-glass-card {{
      display:flex;
      gap:14px;
      align-items:center;
      padding:16px;
      margin-bottom:14px;
      border-radius:22px;
      background:rgba(255,255,255,.18);
      backdrop-filter:blur(16px) saturate(160%);
      border:1px solid rgba(255,255,255,.22);
      box-shadow:0 12px 30px rgba(0,0,0,.22);
    }}

    .employee-avatar {{
      width:44px;
      height:44px;
      border-radius:50%;
      display:flex;
      align-items:center;
      justify-content:center;
      font-size:20px;
      background:rgba(255,255,255,.35);
    }}

    .employee-info {{ flex:1; }}

    .employee-name {{
      font-weight:900;
      font-size:15px;
    }}

    .employee-phone {{
      font-size:13px;
      opacity:.8;
    }}

    .employee-badge {{
      display:inline-block;
      margin-top:6px;
      padding:3px 10px;
      border-radius:999px;
      font-size:12px;
      font-weight:800;
    }}

    .badge-cobrador {{ background:#16a34a;color:white; }}
    .badge-supervisor {{ background:#2563eb;color:white; }}
    .badge-admin {{ background:#7c3aed;color:white; }}
    </style>

    <h2 style="text-align:center;margin-bottom:14px;">
      👥 Empleados
    </h2>

    {cards_html if cards_html else "<p style='text-align:center;'>No hay empleados</p>"}
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )




# ============================================================
# ➕ CREAR USUARIO (FINAL – CON TELÉFONO)
# ============================================================
@app.route("/users/new", methods=["GET", "POST"])
@login_required
@admin_required
def new_user():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        role = request.form.get("role")
        phone = (request.form.get("phone") or "").strip()
        pin = request.form.get("pin")

        if pin != ADMIN_PIN:
            flash("PIN incorrecto.", "danger")
            return redirect(url_for("new_user"))

        if not username or not password or not role:
            flash("Datos incompletos.", "danger")
            return redirect(url_for("new_user"))

        pwd = generate_password_hash(password)

        conn = get_conn()
        cur = conn.cursor()

        try:
            cur.execute("""
                INSERT INTO users (username, password_hash, role, phone, created_at)
                VALUES (%s, %s, %s, %s, %s)
            """, (username, pwd, role, phone, datetime.utcnow()))
            conn.commit()
            flash("Usuario creado correctamente.", "success")

        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            flash("Ese usuario ya existe.", "danger")

        except Exception as e:
            conn.rollback()
            flash(f"Error al crear usuario: {e}", "danger")

        finally:
            cur.close()
            conn.close()

        return redirect(url_for("users"))

    body = """
    <div class="card">
      <h2>➕ Crear usuario</h2>

      <form method="post" style="display:flex;gap:12px;flex-wrap:wrap;">
        <div>
          <label>Usuario</label>
          <input name="username" required>
        </div>

        <div>
          <label>Contraseña</label>
          <input type="password" name="password" required>
        </div>

        <div>
          <label>Teléfono</label>
          <input name="phone" placeholder="8091234567">
        </div>

        <div>
          <label>Rol</label>
          <select name="role" required>
            <option value="cobrador">Cobrador</option>
            <option value="supervisor">Supervisor</option>
            <option value="admin">Admin</option>
          </select>
        </div>

        <div>
          <label>PIN admin</label>
          <input name="pin" required>
        </div>

        <button class="btn btn-primary">Crear usuario</button>
      </form>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=current_user(),
        flashes=get_flashed_messages(with_categories=True),
        app_brand=APP_BRAND,
        admin_whatsapp=ADMIN_WHATSAPP,
        theme=get_theme()
    )





# ============================================================
# 🗑️ BORRAR USUARIO (ADMIN PROTEGIDO)
# ============================================================
@app.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_user(user_id):
    pin = request.form.get("pin")

    if pin != ADMIN_PIN:
        flash("⛔ PIN incorrecto.", "danger")
        return redirect(url_for("users"))

    conn = get_conn()
    cur = conn.cursor()

    # 🔎 Obtener usuario objetivo
    cur.execute("SELECT id, role FROM users WHERE id = %s;", (user_id,))
    target = cur.fetchone()

    if not target:
        flash("Usuario no encontrado.", "danger")
        cur.close()
        conn.close()
        return redirect(url_for("users"))

    # 🔐 Contar admins
    cur.execute("SELECT COUNT(*) AS total_admins FROM users WHERE role = 'admin';")
    total_admins = cur.fetchone()["total_admins"]

    # 🚫 No permitir borrar el último admin
    if target["role"] == "admin" and total_admins <= 1:
        flash("🚫 No se puede borrar el ÚNICO administrador del sistema.", "danger")
        cur.close()
        conn.close()
        return redirect(url_for("users"))

    # ✅ Borrar usuario
    cur.execute("DELETE FROM users WHERE id = %s;", (user_id,))
    conn.commit()

    flash("🗑️ Usuario eliminado correctamente.", "success")

    cur.close()
    conn.close()

    return redirect(url_for("users"))


# ============================================================
#  REASIGNACIÓN MASIVA DE S ENTRE COBRADORES
# ============================================================

@app.route("/reassign", methods=["GET", "POST"])
@login_required
@admin_required
def reassign_clients():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT id, username FROM users WHERE role = 'cobrador';")
    cobradores = cur.fetchall()

    if request.method == "POST":
        from_id = int(request.form.get("from_id"))
        to_id = int(request.form.get("to_id"))

        if from_id == to_id:
            flash("No puedes reasignar al mismo cobrador.", "warning")
            cur.close()
            conn.close()
            return redirect(url_for("reassign_clients"))

        cur.execute("""
            UPDATE clients
            SET created_by = %s
            WHERE created_by = %s;
        """, (to_id, from_id))

        cur.execute("""
            UPDATE loans
            SET created_by = %s
            WHERE created_by = %s;
        """, (to_id, from_id))

        conn.commit()

        flash("s y préstamos reasignados exitosamente.", "success")
        cur.close()
        conn.close()
        return redirect(url_for("reassign_clients"))

    cur.close()
    conn.close()

    opts = "".join([f"<option value='{c['id']}'>{c['username']}</option>" for c in cobradores])

    body = f"""
    <div class='card'>
      <h2>Reasignar s entre cobradores</h2>

      <form method="post">
        <label>Cobrador ORIGEN (quien pierde los s)</label>
        <select name="from_id" required>{opts}</select>

        <label>Cobrador DESTINO (quien recibirá los s)</label>
        <select name="to_id" required>{opts}</select>

        <button class="btn btn-primary" style="margin-top:10px;">Reasignar</button>
      </form>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=current_user(),
        app_brand=APP_BRAND,
        admin_whatsapp=ADMIN_WHATSAPP,
        flashes=get_flashed_messages(with_categories=True),
        theme=get_theme()
    )


# ============================================================
#  REASIGNAR UN SOLO  A OTRO COBRADOR
# ============================================================

@app.route("/clients/<int:client_id>/reassign", methods=["POST"])
@login_required
@role_required("admin", "supervisor")
def reassign_single_client(client_id):
    new_user_id = request.form.get("new_user_id", type=int)
    if not new_user_id:
        flash("Seleccione un cobrador destino.", "danger")
        return redirect(url_for("client_detail", client_id=client_id))

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT id FROM users WHERE id=%s AND role='cobrador';", (new_user_id,))
    row = cur.fetchone()
    if not row:
        flash("Cobrador destino inválido.", "danger")
        cur.close()
        conn.close()
        return redirect(url_for("client_detail", client_id=client_id))

    cur.execute("UPDATE clients SET created_by=%s WHERE id=%s;", (new_user_id, client_id))
    cur.execute("UPDATE loans SET created_by=%s WHERE client_id=%s;", (new_user_id, client_id))
    conn.commit()
    cur.close()
    conn.close()

    flash("Cliente y sus préstamos fueron movidos al nuevo cobrador.", "success")
    return redirect(url_for("client_detail", client_id=client_id))


# ============================================================
# 👥 CLIENTES (RESPETA ROL + FILTRO POR PRESTAMISTA)
# ============================================================
@app.route("/clients")
@login_required
def clients():
    user = current_user()
    conn = get_conn()
    cur = conn.cursor()

    # ===============================
    # PRESTAMISTAS (SOLO ADMIN)
    # ===============================
    selected_lender = request.args.get("lender_id", "all")
    lenders = []

    if user["role"] != "cobrador":
        cur.execute("""
            SELECT id, username
            FROM users
            WHERE role = 'cobrador'
            ORDER BY username
        """)
        lenders = cur.fetchall() or []

    # ===============================
    # CLIENTES (RESPETA ROL)
    # ===============================
    if user["role"] == "cobrador":
        cur.execute("""
            SELECT *
            FROM clients
            WHERE created_by = %s
            ORDER BY id DESC
        """, (user["id"],))
    else:
        if selected_lender == "all":
            cur.execute("""
                SELECT *
                FROM clients
                ORDER BY id DESC
            """)
        else:
            cur.execute("""
                SELECT *
                FROM clients
                WHERE created_by = %s
                ORDER BY id DESC
            """, (selected_lender,))

    rows = cur.fetchall() or []

    cur.close()
    conn.close()

    # ===============================
    # TARJETAS CLIENTE (iOS GLASS)
    # ===============================
    cards_html = "".join([
        f"""
        <div class="client-glass-card"
             onclick="window.location='/clients/{c['id']}'">
          <div class="client-avatar">
            <img src="{c.get('photo') or '/static/no-photo.png'}"
                 style="width:100%;height:100%;object-fit:cover;border-radius:50%;">
          </div>
          <div class="client-info">
            <div class="client-name">
              {c.get('first_name','')} {c.get('last_name','')}
            </div>
            <div class="client-sub">
              📍 {c.get('address') or 'Sin dirección'}
            </div>
            <span class="client-badge badge-green">Activo</span>
          </div>
        </div>
        """
        for c in rows
    ])

    # ===============================
    # FILTRO POR PRESTAMISTA (ADMIN)
    # ===============================
    filtro_html = ""
    if user["role"] != "cobrador":
        options = "".join([
            f"<option value='/clients?lender_id={l['id']}' "
            f"{'selected' if str(l['id']) == str(selected_lender) else ''}>"
            f"{l['username']}</option>"
            for l in lenders
        ])

        filtro_html = f"""
        <label style="font-weight:800;">Ver cliente por prestamista</label>
        <select onchange="location=this.value"
                style="width:100%;margin-bottom:14px;padding:10px;border-radius:14px;">
          <option value="/clients?lender_id=all">-- TODOS --</option>
          {options}
        </select>
        """

    # ===============================
    # HTML FINAL
    # ===============================
    body = f"""
    <style>
    .client-glass-card {{
      display:flex;
      gap:14px;
      padding:16px 14px;
      margin-bottom:14px;
      border-radius:22px;
      background:rgba(255,255,255,.18);
      backdrop-filter:blur(16px) saturate(160%);
      border:1px solid rgba(255,255,255,.22);
      box-shadow:0 12px 30px rgba(0,0,0,.22);
      cursor:pointer;
    }}

    .client-avatar {{
      width:44px;
      height:44px;
      border-radius:50%;
      display:flex;
      align-items:center;
      justify-content:center;
      background:rgba(255,255,255,.35);
    }}

	.mobile-list {{
      height:auto !important;
      overflow-y:auto !important;
      max-height:none !important;
    }}

    .container {{
      height:auto !important;
      max-height:none !important;
      overflow:auto !important;
    }}



    .client-name {{
      font-weight:800;
      font-size:15px;
    }}

    .client-sub {{
      font-size:13px;
      opacity:.85;
    }}

    .client-badge {{
      display:inline-block;
      margin-top:6px;
      padding:3px 10px;
      border-radius:999px;
      font-size:12px;
      font-weight:800;
      background:#16a34a;
      color:white;
    }}

    .btn-ios-glass {{
      display:block;
      width:100%;
      padding:14px 18px;
      margin:10px 0 18px 0;
      text-align:center;
      font-size:16px;
      font-weight:800;
      border-radius:999px;
      text-decoration:none;
      background:rgba(255,255,255,.55);
      color:#0f172a;
    }}
    </style>

    <div class="mobile-list">

      <a href="/clients/new" class="btn-ios-glass">
        ➕ Nuevo cliente
      </a>

      <h2 style="text-align:center;margin-bottom:10px;">
        Lista de Clientes
      </h2>

      {filtro_html}

      {cards_html if cards_html else "<p style='text-align:center;'>No hay clientes</p>"}

    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        app_brand=APP_BRAND,
        admin_whatsapp=ADMIN_WHATSAPP,
        theme=get_theme()
    )
@app.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
@login_required
def edit_client(client_id):
    user = current_user()

    if user["role"] != "admin":
        flash("No tienes permiso para editar clientes.", "danger")
        return redirect(url_for("clients"))

    conn = get_conn()
    cur = conn.cursor()

    if request.method == "POST":

        # ===============================
        # GUARDAR FOTO
        # ===============================
        photo = request.files.get("photo")
        photo_path = None

        if photo and photo.filename:
            filename = secure_filename(photo.filename)
            filename = f"{int(time.time())}_{filename}"

            filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            photo.save(filepath)

            photo_path = "/" + filepath

        # ===============================
        # ACTUALIZAR CLIENTE
        # ===============================
        if photo_path:
            cur.execute("""
                UPDATE clients
                SET first_name=%s,
                    last_name=%s,
                    phone=%s,
                    address=%s,
                    document_id=%s,
                    route=%s,
                    photo=%s
                WHERE id=%s
            """, (
                request.form["first_name"],
                request.form["last_name"],
                request.form["phone"],
                request.form["address"],
                request.form["document_id"],
                request.form["route"],
                photo_path,
                client_id
            ))
        else:
            cur.execute("""
                UPDATE clients
                SET first_name=%s,
                    last_name=%s,
                    phone=%s,
                    address=%s,
                    document_id=%s,
                    route=%s
                WHERE id=%s
            """, (
                request.form["first_name"],
                request.form["last_name"],
                request.form["phone"],
                request.form["address"],
                request.form["document_id"],
                request.form["route"],
                client_id
            ))

        conn.commit()
        cur.close()
        conn.close()

        flash("Cliente actualizado correctamente.", "success")
        return redirect(url_for("client_detail", client_id=client_id))

    cur.execute("SELECT * FROM clients WHERE id=%s", (client_id,))
    client = cur.fetchone()

    cur.close()
    conn.close()
	
    body = f"""
    <div class="card">

    <div style="position:absolute; right:20px; top:20px;">
        <img src="{client['photo'] if client['photo'] else url_for('static', filename='no-photo.png')}"
            style="width:150px;height:150px;border-radius:12px;object-fit:cover;border:1px solid #ccc;">
    </div>

    <h2>✏️ Editar cliente</h2>

    <form method="post" enctype="multipart/form-data">
        <input name="first_name" value="{client['first_name']}" required placeholder="Nombre">
        <input name="last_name" value="{client['last_name']}" required placeholder="Apellido">
        <input name="phone" value="{client['phone']}" placeholder="Teléfono">
        <input name="address" value="{client['address']}" placeholder="Dirección">
        <input name="document_id" value="{client['document_id']}" placeholder="Documento">
        <input name="route" value="{client.get('route','')}" placeholder="Ruta">

        <label>📸 Foto del cliente</label>
        <input type="file" name="photo" accept="image/*" capture="environment">

        <button class="btn btn-primary">💾 Guardar cambios</button>
        <a href="{url_for('client_detail', client_id=client_id)}"
            class="btn btn-secondary">Cancelar</a>
    </form>

    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        flashes=get_flashed_messages(with_categories=True),
        theme=get_theme()
    )


# ============================================================
# ➕ NUEVO CLIENTE
# ============================================================
@app.route("/clients/new", methods=["GET", "POST"])
@login_required
def new_client():
    user = current_user()

    if request.method == "POST":
        first = request.form.get("first_name", "").strip()
        last = request.form.get("last_name", "").strip()
        document_id = request.form.get("document_id", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        route = request.form.get("route", "").strip()

        if not first:
            flash("El nombre es obligatorio.", "danger")
            return redirect("/clients/new")

        # ===============================
        # GUARDAR FOTO
        # ===============================
        photo = request.files.get("photo")
        photo_path = None

        if photo and photo.filename:
            filename = secure_filename(photo.filename)
            filename = f"{int(time.time())}_{filename}"

            filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            photo.save(filepath)

            photo_path = "/" + filepath

        # ===============================
        # GUARDAR CLIENTE
        # ===============================
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO clients
            (first_name, last_name, document_id, phone, address, route, created_by, photo)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            first,
            last,
            document_id,
            phone,
            address,
            route,
            user["id"],
            photo_path
        ))

        conn.commit()
        cur.close()
        conn.close()

        flash("Cliente creado correctamente.", "success")
        return redirect("/clients")

    body = """
    <div class="card">
      <h2>➕ Nuevo cliente</h2>

      <form method="post" enctype="multipart/form-data">
        <label>Nombre</label>
        <input name="first_name" required>

        <label>Apellido</label>
        <input name="last_name">

        <label>Cédula / ID</label>
        <input name="document_id" placeholder="Ej: 001-1234567-8">

        <label>Teléfono</label>
        <input name="phone">

        <label>Dirección</label>
        <input name="address">

        <label>Ruta</label>
        <input name="route">

		<label>Foto del cliente</label>
        <input type="file" name="photo" accept="image/*" capture="environment">

        <button class="btn btn-primary" style="margin-top:10px;">
          Guardar
        </button>
      </form>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        app_brand=APP_BRAND,
        admin_whatsapp=ADMIN_WHATSAPP,
        theme=get_theme()
    )


@app.route("/clients/<int:client_id>")
@login_required
def client_detail(client_id):
    user = current_user()
    conn = get_conn()
    cur = conn.cursor()

    # ===============================
    # CLIENTE
    # ===============================
    cur.execute("SELECT * FROM clients WHERE id=%s", (client_id,))
    client = cur.fetchone()

    if not client:
        flash("Cliente no encontrado.", "danger")
        cur.close()
        conn.close()
        return redirect(url_for("clients"))

    if user["role"] == "cobrador" and client["created_by"] != user["id"]:
        flash("No tienes permiso para este cliente.", "danger")
        cur.close()
        conn.close()
        return redirect(url_for("clients"))

    # ===============================
    # PRÉSTAMOS
    # ===============================
    cur.execute("""
        SELECT id, amount, remaining, rate, frequency,
               start_date, total_interest_paid, status, term_count
        FROM loans
        WHERE client_id=%s
        ORDER BY id DESC
    """, (client_id,))
    loans = cur.fetchall()

    # ===============================
    # REASIGNAR COBRADOR
    # ===============================
    reassign_block = ""
    if user["role"] in ("admin", "supervisor"):
        cur.execute("""
            SELECT id, username
            FROM users
            WHERE role='cobrador'
            ORDER BY username
        """)
        cobradores = cur.fetchall()

        options = "".join(
            f"<option value='{u['id']}'>{u['username']}</option>"
            for u in cobradores
        )

        reassign_block = f"""
        <form method="post"
              action="{url_for('reassign_single_client', client_id=client_id)}"
              style="margin-top:12px;">
          <label>Reasignar a cobrador:</label>
          <select name="new_user_id" required>
            <option value="">--Seleccione--</option>
            {options}
          </select>
          <button class="btn btn-primary" style="margin-left:8px;">
            Mover cliente
          </button>
        </form>
        """

    cur.close()
    conn.close()

    # ===============================
    # TABLA DE PRÉSTAMOS
    # ===============================
    loans_html = "".join([
        f"""
        <tr>
          <td>{l['id']}</td>
          <td>{fmt_money(l['amount'])}</td>
          <td>{fmt_money(l['remaining'])}</td>
          <td>{l['rate']}%</td>
          <td>{l['frequency']}</td>
          <td>{l['start_date']}</td>
          <td>{fmt_money(l.get('total_interest_paid', 0))}</td>
          <td>{l.get('status', 'activo')}</td>

          <td style="display:flex; gap:6px; flex-wrap:wrap;">
            <a class="btn btn-secondary"
               href="{url_for('loan_detail', loan_id=l['id'])}">
               Ver
            </a>

            {
              ""
              if user["role"] not in ["admin", "supervisor"]
              else f'''
              <a class="btn btn-warning"
                 href="{url_for('edit_loan', loan_id=l['id'])}">
                 ✏️ Editar
              </a>
              '''
            }

            {
              ""
              if user["role"] != "admin"
              else f'''
              <form method="post"
                    action="{url_for('delete_loan', loan_id=l['id'])}"
                    onsubmit="return confirm('¿Seguro que deseas eliminar este préstamo?');">
                <button class="btn btn-danger">Eliminar</button>
              </form>
              '''
            }
          </td>
        </tr>
        """
        for l in loans
    ])

    # ===============================
    # ACCIONES CLIENTE (ADMIN)
    # ===============================
    actions_block = ""
    if user["role"] == "admin":
        actions_block = f"""
        <div style="display:flex; gap:12px; margin:14px 0; flex-wrap:wrap;">
          <a href="{url_for('edit_client', client_id=client_id)}"
             class="btn btn-warning">
             ✏️ Editar cliente
          </a>

          <form method="post"
                action="{url_for('delete_client', client_id=client_id)}"
                onsubmit="return confirm('⚠️ Esto eliminará el cliente y TODOS sus préstamos. ¿Seguro?');"
                style="display:flex; gap:6px;">
            <input name="pin" placeholder="PIN" required style="width:80px;">
            <button class="btn btn-danger">
              🗑️ Eliminar cliente
            </button>
          </form>
        </div>
        """

    # ===============================
    # HTML FINAL
    # ===============================
    photo = client.get("photo") or "/static/no-photo.png"

    body = f"""
    <div class="card">
      <div style="
        display:flex;
        justify-content:space-between;
        align-items:flex-start;
        gap:30px;
      ">

        <div>
          <h2>Cliente {client['first_name']} {client['last_name']}</h2>
          <p>Tel: {client['phone']}</p>
          <p>Dirección: {client['address']}</p>
          <p>Documento: {client['document_id']}</p>
          <p>Ruta: {client.get('route','')}</p>

          {actions_block}
          {reassign_block}
        </div>

        <div style="
          width:160px;
          height:180px;
          border-radius:12px;
          overflow:hidden;
          border:3px solid #e3e3e3;
          background:#f5f5f5;
          display:flex;
          align-items:center;
          justify-content:center;
        ">
          <img src="{photo}"
               style="width:100%;height:100%;object-fit:cover;">
        </div>

      </div>

    </div>

    <div class="card">
      <h3>Préstamos</h3>
      <a class="btn btn-primary"
         href="/loans/new?client_id={client_id}">
         ➕ Nuevo préstamo
      </a>

      <div class="table-wrapper">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Monto</th>
              <th>Restante</th>
              <th>%</th>
              <th>Frecuencia</th>
              <th>Inicio</th>
              <th>Interés pagado</th>
              <th>Estado</th>
              <th>Acciones</th>
            </tr>
          </thead>
          <tbody>
            {loans_html}
          </tbody>
        </table>
      </div>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        flashes=get_flashed_messages(with_categories=True),
        theme=get_theme()
    )


# ============================================================
# 🗑️ ELIMINAR PRÉSTAMO + REVERSO FINANCIERO CORRECTO
# ============================================================
@app.route("/loans/<int:loan_id>/delete", methods=["POST"])
@login_required
def delete_loan(loan_id):

    user = current_user()

    if user["role"] != "admin":
        flash("No tienes permiso.", "danger")
        return redirect(request.referrer or "/clients")

    conn = None
    cur = None

    try:
        conn = get_conn()
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # ============================
        # OBTENER PRESTAMO
        # ============================
        cur.execute("SELECT amount, created_by FROM loans WHERE id=%s", (loan_id,))
        loan = cur.fetchone()

        if not loan:
            flash("Préstamo no encontrado.", "danger")
            return redirect(request.referrer)

        loan_amount = float(loan["amount"] or 0)
        created_by = loan["created_by"]

        # ============================
        # TOTAL PAGOS
        # ============================
        cur.execute("""
            SELECT COALESCE(SUM(amount),0) total
            FROM payments
            WHERE loan_id=%s AND status <> 'ANULADO'
        """, (loan_id,))
        total_pagado = float(cur.fetchone()["total"] or 0)

        # ============================
        # DESCUENTO INICIAL
        # ============================
        cur.execute("""
            SELECT COALESCE(SUM(amount),0) total
            FROM initial_discounts
            WHERE loan_id=%s
        """, (loan_id,))
        discount = float(cur.fetchone()["total"] or 0)

        # ============================
        # BORRAR PAGOS Y DESCUENTOS
        # ============================
        cur.execute("DELETE FROM payments WHERE loan_id=%s", (loan_id,))
        cur.execute("DELETE FROM initial_discounts WHERE loan_id=%s", (loan_id,))

        # ============================
        # BORRAR PRÉSTAMO
        # ============================
        cur.execute("DELETE FROM loans WHERE id=%s", (loan_id,))

        # ============================
        # DEVOLVER CAPITAL AL BANCO
        # ============================
        if loan_amount > 0:
            cur.execute("""
                INSERT INTO cash_reports
                (user_id, movement_type, amount, note, created_at)
                VALUES (%s,'reverso_prestamo',%s,%s,NOW())
            """, (
                created_by,
                loan_amount,
                f"Reverso préstamo #{loan_id}"
            ))

        # ============================
        # REVERSAR DESCUENTO (BAJA BANCO)
        # ============================
        if discount > 0:
            cur.execute("""
                INSERT INTO cash_reports
                (user_id, movement_type, amount, note, created_at)
                VALUES (%s,'reverso_descuento',%s,%s,NOW())
            """, (
                created_by,
                -discount,
                f"Reverso descuento #{loan_id}"
            ))

        # ============================
        # REVERSAR PAGOS (BAJA BANCO)
        # ============================
        if total_pagado > 0:
            cur.execute("""
                INSERT INTO cash_reports
                (user_id, movement_type, amount, note, created_at)
                VALUES (%s,'reverso_pago',%s,%s,NOW())
            """, (
                created_by,
                -total_pagado,
                f"Reverso pagos #{loan_id}"
            ))

        conn.commit()
        flash("✅ Préstamo eliminado y banco corregido automáticamente.", "success")

    except Exception as e:
        print("ERROR ELIMINAR PRESTAMO:", e)
        if conn:
            conn.rollback()
        flash("Error eliminando préstamo.", "danger")

    finally:
        if cur: cur.close()
        if conn: conn.close()

    return redirect(request.referrer or "/clients")



@app.route("/loans/<int:loan_id>/edit", methods=["GET", "POST"])
@login_required
def edit_loan(loan_id):
    user = current_user()

    if user["role"] not in ["admin", "supervisor"]:
        flash("No tienes permiso para editar préstamos.", "danger")
        return redirect(url_for("clients"))

    conn = get_conn()
    cur = conn.cursor()

    # ===============================
    # OBTENER PRÉSTAMO (SIEMPRE)
    # ===============================
    cur.execute("""
        SELECT id, amount, rate, frequency, client_id
        FROM loans
        WHERE id=%s
    """, (loan_id,))
    loan = cur.fetchone()

    if not loan:
        cur.close()
        conn.close()
        flash("Préstamo no encontrado.", "danger")
        return redirect(url_for("clients"))

    # ===============================
    # GUARDAR CAMBIOS (POST)
    # ===============================
    if request.method == "POST":
        cur.execute("""
            UPDATE loans
            SET amount=%s,
                rate=%s,
                frequency=%s
            WHERE id=%s
        """, (
            request.form["amount"],
            request.form["rate"],
            request.form["frequency"],
            loan_id
        ))

        conn.commit()
        cur.close()
        conn.close()

        flash("Préstamo actualizado correctamente.", "success")
        return redirect(
            url_for("client_detail", client_id=loan["client_id"])
        )

    cur.close()
    conn.close()

    # ===============================
    # FORMULARIO (GET)
    # ===============================
    body = f"""
    <div class="card">
      <h2>✏️ Editar préstamo #{loan_id}</h2>

      <form method="post">
        <label>Monto</label>
        <input name="amount" value="{loan['amount']}" required>

        <label>Interés (%)</label>
        <input name="rate" value="{loan['rate']}" required>

        <label>Frecuencia</label>
        <input name="frequency" value="{loan['frequency']}" required>

        <button class="btn btn-success">💾 Guardar cambios</button>
        <a href="{url_for('client_detail', client_id=loan['client_id'])}"
           class="btn btn-secondary">Cancelar</a>
      </form>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        flashes=get_flashed_messages(with_categories=True),
        theme=get_theme()
    )




# ============================================================
# 🗑️ ELIMINAR CLIENTE + REVERSO FINANCIERO CORRECTO
# ============================================================
@app.route("/clients/<int:client_id>/delete", methods=["POST"])
@login_required
def delete_client(client_id):

    user = current_user()

    if user["role"] != "admin":
        flash("No tienes permiso.", "danger")
        return redirect(url_for("clients"))

    conn = None
    cur = None

    try:
        conn = get_conn()
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # ============================
        # OBTENER PRÉSTAMOS DEL CLIENTE
        # ============================
        cur.execute("""
            SELECT id, amount, created_by
            FROM loans
            WHERE client_id=%s
        """, (client_id,))
        loans = cur.fetchall() or []

        # ============================
        # REVERSAR CADA PRÉSTAMO
        # ============================
        for loan in loans:

            loan_id = loan["id"]
            loan_amount = float(loan["amount"] or 0)
            created_by = loan["created_by"]

            # ---- total pagos
            cur.execute("""
                SELECT COALESCE(SUM(amount),0) total
                FROM payments
                WHERE loan_id=%s AND status <> 'ANULADO'
            """, (loan_id,))
            total_pagado = float(cur.fetchone()["total"] or 0)

            # ---- descuento inicial
            cur.execute("""
                SELECT COALESCE(SUM(amount),0) total
                FROM initial_discounts
                WHERE loan_id=%s
            """, (loan_id,))
            discount = float(cur.fetchone()["total"] or 0)

            # ============================
            # BORRAR PAGOS Y DESCUENTOS
            # ============================
            cur.execute("DELETE FROM payments WHERE loan_id=%s", (loan_id,))
            cur.execute("DELETE FROM initial_discounts WHERE loan_id=%s", (loan_id,))

            # ============================
            # BORRAR PRÉSTAMO
            # ============================
            cur.execute("DELETE FROM loans WHERE id=%s", (loan_id,))

            # ============================
            # DEVOLVER CAPITAL AL BANCO
            # ============================
            if loan_amount > 0:
                cur.execute("""
                    INSERT INTO cash_reports
                    (user_id, movement_type, amount, note, created_at)
                    VALUES (%s,'reverso_prestamo',%s,%s,NOW())
                """, (
                    created_by,
                    loan_amount,
                    f"Reverso préstamo #{loan_id} por eliminar cliente"
                ))

            # ============================
            # REVERSAR DESCUENTO (BAJA BANCO)
            # ============================
            if discount > 0:
                cur.execute("""
                    INSERT INTO cash_reports
                    (user_id, movement_type, amount, note, created_at)
                    VALUES (%s,'reverso_descuento',%s,%s,NOW())
                """, (
                    created_by,
                    -discount,
                    f"Reverso descuento #{loan_id}"
                ))

            # ============================
            # REVERSAR PAGOS (BAJA BANCO)
            # ============================
            if total_pagado > 0:
                cur.execute("""
                    INSERT INTO cash_reports
                    (user_id, movement_type, amount, note, created_at)
                    VALUES (%s,'reverso_pago',%s,%s,NOW())
                """, (
                    created_by,
                    -total_pagado,
                    f"Reverso pagos #{loan_id}"
                ))

        # ============================
        # BORRAR CLIENTE
        # ============================
        cur.execute("DELETE FROM clients WHERE id=%s", (client_id,))

        conn.commit()
        flash("✅ Cliente eliminado y banco corregido automáticamente.", "success")

    except Exception as e:
        print("ERROR ELIMINAR CLIENTE:", e)
        if conn:
            conn.rollback()
        flash("Error eliminando cliente.", "danger")

    finally:
        if cur: cur.close()
        if conn: conn.close()

    return redirect(url_for("clients"))



# ============================================================
# 📋 LISTA DE PRÉSTAMOS (iOS GLASS + FILTROS + ROLES)
# ============================================================
@app.route("/loans")
@login_required
def loans():
    user = current_user()
    conn = get_conn()
    cur = conn.cursor()

    filter_user = request.args.get("filter_user", type=int)

    # ===============================
    # QUERY PRINCIPAL (SEGÚN ROL)
    # ===============================
    if user["role"] == "cobrador":
        cur.execute("""
            SELECT l.id, l.remaining, l.status, l.frequency,
       c.first_name, c.last_name
            FROM loans l
            JOIN clients c ON c.id = l.client_id
            WHERE l.created_by = %s
            ORDER BY l.id DESC
            LIMIT 100
        """, (user["id"],))
        rows = cur.fetchall() or []
        resumen_html = ""

    else:
        if filter_user:
            cur.execute("""
                SELECT l.id, l.remaining, l.status, l.frequency,
       c.first_name, c.last_name
                FROM loans l
                JOIN clients c ON c.id = l.client_id
                WHERE l.created_by = %s
                ORDER BY l.id DESC
                LIMIT 100
            """, (filter_user,))
        else:
            cur.execute("""
                SELECT l.id, l.remaining, l.status, l.frequency,
       c.first_name, c.last_name
                FROM loans l
                JOIN clients c ON c.id = l.client_id
                ORDER BY l.id DESC
                LIMIT 100
            """)
        rows = cur.fetchall() or []


        # ===============================
        # RESUMEN POR PRESTAMISTA
        # ===============================
        if filter_user:
                cur.execute("""
                    SELECT 
                        u.username,

                        COUNT(
                            CASE 
                                WHEN l.remaining > 0 
                                THEN l.id 
                            END
                        ) AS total_loans,

                        COUNT(
                            CASE 
                                WHEN l.remaining <= 0 
                                THEN l.id 
                            END
                        ) AS loans_pagados,

                        COALESCE(SUM(l.amount),0) AS total_prestado,

                        COALESCE(SUM(
                            CASE 
                                WHEN l.remaining > 0 
                                THEN l.remaining 
                            END
                        ),0) AS capital_activo

                    FROM users u
                    LEFT JOIN loans l ON l.created_by = u.id
                    WHERE u.id = %s
                    GROUP BY u.username
                """, (filter_user,))
        else:
                cur.execute("""
                    SELECT 
                        u.username,

                        COUNT(
                            CASE 
                                WHEN l.remaining > 0 
                                THEN l.id 
                            END
                        ) AS total_loans,

                        COUNT(
                            CASE 
                                WHEN l.remaining <= 0 
                                THEN l.id 
                            END
                        ) AS loans_pagados,

                        COALESCE(SUM(l.amount),0) AS total_prestado,

                        COALESCE(SUM(
                            CASE 
                                WHEN l.remaining > 0 
                                THEN l.remaining 
                            END
                        ),0) AS capital_activo

                    FROM users u
                    LEFT JOIN loans l ON l.created_by = u.id
                    WHERE u.role = 'cobrador'
                    GROUP BY u.username
                    ORDER BY u.username
                """)
        resumen = cur.fetchall() or []

        resumen_rows = "".join(
            f"""
            <tr>
              <td>{r['username']}</td>
              <td>{r['total_loans']}</td>
              <td>{r['loans_pagados']}</td>
              <td>{fmt_money(r['total_prestado'])}</td>
              <td>{fmt_money(r['capital_activo'])}</td>
            </tr>
            """ for r in resumen
        )

        cur.execute("SELECT id, username FROM users WHERE role='cobrador' ORDER BY username")
        cobradores = cur.fetchall() or []

        filter_opts = "<option value=''>-- TODOS --</option>" + "".join(
            f"<option value='{c['id']}' {'selected' if filter_user==c['id'] else ''}>{c['username']}</option>"
            for c in cobradores
        )

        resumen_html = f"""
        <div class="card">
          <h3>👤 Ver préstamos por prestamista</h3>
          <select onchange="location.href='/loans' + (this.value ? ('?filter_user=' + this.value) : '')">
            {filter_opts}
          </select>
        </div>

        <div class="card">
          <h3>📌 Resumen por prestamista</h3>
          <table>
            <tr>
              <th>Prestamista</th>
              <th># Préstamos</th>
              <th>Pagados</th>
              <th>Total prestado</th>
              <th>Capital activo</th>
            </tr>
            {resumen_rows or "<tr><td colspan='4'>Sin datos</td></tr>"}
          </table>
        </div>
        """   

    # ===============================
    # CERRAR BD
    # ===============================
    cur.close()
    conn.close()

    # ===============================
    # CARDS iOS GLASS
    # ===============================
    cards_html = "".join([
        f"""
        <a href="{url_for('loan_detail', loan_id=l['id'])}" style="text-decoration:none;color:inherit;">
          <div class="loan-glass-card">
            <div class="loan-avatar">💵</div>
            <div class="loan-info">
              <div class="loan-name">{l['first_name']} {l['last_name']}</div>
              <div class="loan-sub">
                Préstamo #{l['id']} · {(l.get('frequency') or '').capitalize()}
              </div>
              <span class="loan-badge {'badge-green' if l.get('status')=='activo' else 'badge-red'}">
                {l.get('status','activo').capitalize()}
              </span>
            </div>
            <div class="loan-amount">{fmt_money(l.get('remaining',0))}</div>
          </div>
        </a>
        """ for l in rows
    ])

    body = f"""
    <style>
    .loan-glass-card {{
      display:flex;gap:14px;align-items:center;
      padding:16px;margin-bottom:14px;border-radius:22px;
      background:rgba(255,255,255,.18);
      backdrop-filter:blur(16px) saturate(160%);
      border:1px solid rgba(255,255,255,.22);
      box-shadow:0 12px 30px rgba(0,0,0,.22);
    }}
    .loan-avatar {{ width:44px;height:44px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:22px;background:rgba(255,255,255,.35); }}
    .loan-info {{ flex:1; }}
    .loan-name {{ font-weight:900; }}
    .loan-sub {{ font-size:13px;opacity:.8; }}
    .loan-badge {{ margin-top:6px;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:800;display:inline-block; }}
    .badge-green {{ background:#16a34a;color:#fff; }}
    .badge-red {{ background:#dc2626;color:#fff; }}
    .loan-amount {{ font-weight:900;white-space:nowrap;color:#065f46; }}
    </style>

    <h2 style="text-align:center;margin-bottom:14px;">📋 Lista de Préstamos</h2>

    <a class="btn btn-primary" style="width:100%;margin-bottom:14px;" href="{url_for('new_loan')}">
     ➕ Nuevo préstamo
    </a>
    
    {resumen_html}
    {cards_html or "<p style='text-align:center;'>No hay préstamos</p>"}
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )



from datetime import datetime, date


from psycopg2.extras import RealDictCursor

# ============================================================
# 💰 CREAR PRÉSTAMO — FINANCIERO REAL CORREGIDO
# ✔ préstamo baja banco
# ✔ descuento inicial sube banco
# ✔ descuento se guarda en initial_discounts
# ✔ compatible con delete_loan
# ============================================================
@app.route("/loans/new", methods=["GET", "POST"])
@login_required
def new_loan():

    from psycopg2.extras import RealDictCursor
    from datetime import datetime, timedelta, date

    user = current_user()
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:

        # =====================================================
        # 🏦 SALDO BANCO ACTUAL
        # =====================================================
        cur.execute("SELECT COALESCE(SUM(amount),0) AS total FROM cash_reports")
        bank_balance = float(cur.fetchone()["total"] or 0)

        # =====================================================
        # CLIENTES
        # =====================================================
        if user.get("role") == "admin":
            cur.execute("SELECT id, first_name, last_name FROM clients ORDER BY first_name")
        else:
            cur.execute("""
                SELECT id, first_name, last_name
                FROM clients
                WHERE created_by=%s
                ORDER BY first_name
            """, (user["id"],))

        clients = cur.fetchall()

        # =====================================================
        # CREAR PRÉSTAMO
        # =====================================================
        if request.method == "POST":

            client_id = request.form.get("client_id", type=int)
            amount = request.form.get("amount", type=float)
            rate = request.form.get("rate", type=float) or 0
            freq = request.form.get("frequency")
            start_str = request.form.get("start_date")
            term_count = request.form.get("term_count", type=int) or 1
            upfront_percent = request.form.get("upfront_percent", type=float) or 0

            # ===============================
            # VALIDACIONES
            # ===============================
            if not client_id or not amount or not start_str:
                flash("Todos los campos son obligatorios", "danger")
                return redirect(request.url)

            if amount <= 0:
                flash("Monto inválido", "danger")
                return redirect(request.url)

            if bank_balance < amount:
                flash(f"Banco insuficiente. Disponible: {fmt_money(bank_balance)}", "danger")
                return redirect(request.url)

            upfront_percent = max(0, min(upfront_percent, 50))

            # ===============================
            # CÁLCULOS
            # ===============================
            total_interest = round((amount * rate / 100) * term_count, 2)
            total_to_pay = round(amount + total_interest, 2)
            installment_amount = round(total_to_pay / term_count, 2)

            discount_amount = round(amount * upfront_percent / 100, 2)

            if discount_amount > 0:
                total_to_pay -= discount_amount

            # ===============================
            # FECHAS
            # ===============================
            from datetime import datetime, timedelta

            # convertir a DATE limpio (sin hora)
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date()

            # calcular PRIMERA fecha de pago desde start_date
            if freq == "diario":
                next_payment_date = start_date + timedelta(days=1)

            elif freq == "semanal":

                # Siempre próximo sábado real
                days_until_saturday = (5 - start_date.weekday()) % 7

                if days_until_saturday == 0:
                    days_until_saturday = 7

                next_payment_date = start_date + timedelta(days=days_until_saturday)

            elif freq == "quincenal":
                next_payment_date = start_date + timedelta(days=14)

            elif freq == "mensual":
                next_payment_date = start_date + timedelta(days=30)

            else:
                next_payment_date = start_date + timedelta(days=7)
				
            # =====================================================
            # 💰 1️⃣ RESTAR DINERO DEL BANCO (ENTREGA PRÉSTAMO)
            # =====================================================
            cur.execute("""
                INSERT INTO cash_reports (
                    user_id,
                    movement_type,
                    amount,
                    note,
                    created_at
                )
                VALUES (%s,'prestamo_entregado',%s,%s,NOW())
            """, (
                user["id"],
                -abs(amount),
                f"Préstamo entregado cliente {client_id}"
            ))

            # =====================================================
            # 2️⃣ CREAR PRÉSTAMO
            # =====================================================
            cur.execute("""
                INSERT INTO loans (
                    client_id,
                    amount,
                    rate,
                    frequency,
                    start_date,
                    next_payment_date,
                    created_by,
                    remaining_capital,
                    remaining,
                    total_interest_paid,
                    total_interest,
                    total_to_pay,
                    status,
                    term_count,
                    upfront_percent,
                    installment_amount
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ACTIVO',%s,%s,%s)
                RETURNING id
            """, (
                client_id,
                amount,
                rate,
                freq,
                start_date,
                next_payment_date,
                user["id"],
                amount,
                total_to_pay,
                0,
                total_interest,
                total_to_pay,
                term_count,
                upfront_percent,
                installment_amount
            ))

            loan_id = cur.fetchone()["id"]

            # =====================================================
            # 3️⃣ DESCUENTO INICIAL → SUBE BANCO + GUARDA ACTA
            # =====================================================
            if discount_amount > 0:

                collector_id = user["id"]
                route_id = None

                # SUBE BANCO
                cur.execute("""
                    INSERT INTO cash_reports (
                        user_id,
                        movement_type,
                        amount,
                        note,
                        created_at
                    )
                    VALUES (%s,'descuento_inicial',%s,%s,NOW())
                """, (
                    collector_id,
                    discount_amount,
                    f"Descuento inicial préstamo #{loan_id}"
                ))

                # GUARDA EN ACTA GLOBAL
                cur.execute("""
                    INSERT INTO initial_discounts (
                        collector_id,
                        route_id,
                        loan_id,
                        amount,
                        created_at
                    )
                    VALUES (%s,%s,%s,%s,NOW())
                """, (
                    collector_id,
                    route_id,
                    loan_id,
                    discount_amount
                ))
           
            # ===============================
            # GUARDAR
            # ===============================
            conn.commit()

            flash("✅ Préstamo creado correctamente", "success")
            return redirect("/loans")

        # =====================================================
        # FORMULARIO
        # =====================================================
        options = "".join([
            f"<option value='{c['id']}'>{c['first_name']} {c['last_name']}</option>"
            for c in clients
        ])

        body = f"""
        <div class="card">
            <h2>Nuevo préstamo</h2>

            <div style="background:#eef2ff;padding:10px;border-radius:8px;margin-bottom:20px">
                🏦 Banco disponible: <b>{fmt_money(bank_balance)}</b>
            </div>

            <form method="post">

                <label>Cliente</label>
                <select name="client_id">{options}</select>

                <label>Monto</label>
                <input type="number" step="0.01" name="amount" required>

                <label>Interés %</label>
                <input type="number" step="0.01" name="rate" value="0">

                <label>Descuento inicial (%)</label>
                <input type="number" step="0.01" name="upfront_percent" value="0">

                <label>Frecuencia</label>
                <select name="frequency">
                    <option value="diario">Diario</option>
                    <option value="semanal">Semanal</option>
                    <option value="quincenal">Quincenal</option>
                    <option value="mensual">Mensual</option>
                </select>

                <label>Fecha inicio</label>
                <input type="date" name="start_date" value="{date.today()}">

                <label>Cuotas</label>
                <input type="number" name="term_count" value="1">

                <button class="btn btn-primary">Guardar</button>

            </form>
        </div>
        """

        return render_template_string(
            TPL_LAYOUT,
            body=body,
            user=user,
            flashes=get_flashed_messages(with_categories=True),
            admin_whatsapp=ADMIN_WHATSAPP,
            app_brand=APP_BRAND,
            theme=get_theme()
        )

    except Exception as e:
        conn.rollback()
        flash(f"Error creando préstamo: {str(e)}", "danger")
        return redirect(request.url)

    finally:
        cur.close()
        conn.close()


# ============================================================
#  DETALLE DE PRÉSTAMO + HISTORIAL + ALERTA + WHATSAPP
# ============================================================
from datetime import date, timedelta
from urllib.parse import quote

@app.route("/loan/<int:loan_id>")
@login_required
def loan_detail(loan_id):
    user = current_user()

    # 🔒 SOLO ADMIN VE EL BOTÓN ELIMINAR
    puede_eliminar = (user["role"] == "admin")

    conn = get_conn()
    cur = conn.cursor()


    # ===============================
    # PRÉSTAMO
    # ===============================
    cur.execute("""
        SELECT l.*, c.first_name, c.last_name, c.phone
        FROM loans l
        JOIN clients c ON c.id = l.client_id
        WHERE l.id = %s
    """, (loan_id,))
    loan = cur.fetchone()

    if not loan:
        flash("Préstamo no encontrado.", "danger")
        cur.close()
        conn.close()
        return redirect(url_for("loans"))

    phone = (loan.get("phone") or "").strip()
    amount = float(loan.get("amount") or 0)
    rate = float(loan.get("rate") or 0)
    term_count = int(loan.get("term_count") or 1)
    frequency = loan.get("frequency") or "semanal"
    start_date = loan.get("start_date")  # puede ser date o None

    # ===============================
    # DESCUENTO ADELANTO
    # ===============================
    upfront_percent = loan.get("upfront_percent") or 0
    upfront_percent = max(0, min(float(upfront_percent or 0), 50))

    upfront_discount = amount * upfront_percent / 100
    amount_delivered = amount - upfront_discount

    # ===============================
    # DATOS BASE
    # ===============================
    effective_amount = amount_delivered

    capital_per_installment = effective_amount / max(term_count, 1)
    interest_per_installment = effective_amount * rate / 100

    total_interest = interest_per_installment * term_count
    total_to_pay = effective_amount + total_interest
    installment = capital_per_installment + interest_per_installment

    # ===============================
    # PAGOS
    # ===============================
    cur.execute("""
        SELECT id, amount, date
        FROM payments
        WHERE loan_id = %s
        ORDER BY date ASC, id ASC
    """, (loan_id,))
    payments_all = cur.fetchall() or []

    # COPIA SEGURA PARA CÁLCULOS
    payments_calc = payments_all[:]

    # ===============================
    # CÁLCULO DE PAGOS
    # ===============================
    total_paid = 0.0
    total_capital_paid = 0.0
    total_interest_paid = 0.0
    remaining_capital = float(effective_amount)
    
    # ============================================================
    # 🔒 BLINDAJE FINANCIERO (NO TOCAR LO DEMÁS)
    # - El descuento NO reduce deuda
    # - El interés SIEMPRE sobre capital aprobado
    # ============================================================

    # Forzar capital real para cálculos
    effective_amount = amount            # 👈 CAPITAL APROBADO

    # Recalcular correctamente
    capital_per_installment = effective_amount / max(term_count, 1)
    interest_per_installment = effective_amount * rate / 100

    total_interest = interest_per_installment * term_count
    total_to_pay = effective_amount + total_interest
    installment = capital_per_installment + interest_per_installment
       




    for p in payments_calc:
        pago = float((p.get("amount") or 0))
        total_paid += pago

        interes = min(float(interest_per_installment), pago)
        total_interest_paid += interes

        capital = min(max(pago - interes, 0), remaining_capital)
        total_capital_paid += capital
        remaining_capital -= capital

    # ===============================
    # CUOTAS PAGADAS CORRECTAMENTE
    # ===============================

    cuotas_pagadas = int(total_paid // installment)
    cuotas_pagadas = min(cuotas_pagadas, term_count)

    cuotas_restantes = max(term_count - cuotas_pagadas, 0)

    cuotas_resumen = f"{cuotas_pagadas}/{term_count}"

    pago_label = {
        "diario": "día",
        "semanal": "semana",
        "quincenal": "quincena",
        "mensual": "mes"
    }.get(frequency, "período")

    # ===============================
    # FECHA FIN (BLINDAJE start_date)
    # ===============================
    end_date = loan.get("end_date")

    # Si start_date está vacío, no calculamos fechas
    if start_date and not end_date:
        if frequency == "diario":
            end_date = start_date + timedelta(days=term_count)
        elif frequency == "semanal":
            end_date = start_date + timedelta(weeks=term_count)
        elif frequency == "quincenal":
            end_date = start_date + timedelta(days=14 * term_count)
        elif frequency == "mensual":
            end_date = start_date + timedelta(days=30 * term_count)

    # days_left SIEMPRE definido
    if end_date:
        try:
            days_left = (end_date - date.today()).days
        except:
            days_left = "-"
    else:
        days_left = "-"

    # ===============================
    # PRÓXIMO PAGO + ALERTA 🔴
    # ===============================
    proximo_pago_fecha = None
    proximo_pago_estado = "Pendiente"
    alerta_html = ""

    # ✅ ESTE ERA EL QUE FALTABA:
    proximo_pago_monto = installment

    if cuotas_pagadas >= term_count or remaining_capital <= 0:
        proximo_pago_estado = "Préstamo saldado"
        proximo_pago_fecha = None
    else:

        # ===============================
        # USAR FECHA REAL DEL PRÉSTAMO (FIX)
        # ===============================
        proximo_pago_fecha = loan.get("next_payment_date")

        if proximo_pago_fecha and isinstance(proximo_pago_fecha, str):
            from datetime import datetime
            proximo_pago_fecha = datetime.strptime(
                proximo_pago_fecha, "%Y-%m-%d"
            ).date()

        if proximo_pago_fecha:
            if proximo_pago_fecha < date.today():
                proximo_pago_estado = "VENCIDO"
                alerta_html = f"""
                <div style="background:#fee2e2;color:#991b1b;
                            padding:12px;border-radius:12px;
                            font-weight:900;margin:12px 0;">
                  🔴 PAGO VENCIDO — debía pagarse el {proximo_pago_fecha.strftime('%d/%m/%Y')}
                </div>
                """
        else:
            proximo_pago_estado = "Sin fecha programada"

    proximo_pago_txt = proximo_pago_fecha.strftime("%d/%m/%Y") if proximo_pago_fecha else "—"

    # Color para la fecha del próximo pago
    color_fecha_pago = "#16a34a"
    if proximo_pago_estado == "VENCIDO":
        color_fecha_pago = "#dc2626"
    if proximo_pago_estado in ("Préstamo saldado", "Sin fecha programada"):
        color_fecha_pago = "#6b7280"

    # ===============================
    # WHATSAPP RECORDATORIO 📲
    # ===============================
    recordatorio_texto = f"""
🔔 RECORDATORIO DE PAGO

Cliente: {loan['first_name']} {loan['last_name']}
Préstamo #{loan_id}

Próximo pago: {proximo_pago_txt}
Monto: {fmt_money(proximo_pago_monto)}

Saldo pendiente: {fmt_money(remaining_capital)}

JDM Cash Now
""".strip()

    whatsapp_recordatorio = "#"
    if phone:
        whatsapp_recordatorio = f"https://wa.me/{phone}?text={quote(recordatorio_texto)}"
    # ===============================
    # HISTORIAL HTML (TODOS LOS PAGOS)
    # ===============================
    history_rows = ""

    for i, p in enumerate(payments_all, start=1):
        pago = float(p.get("amount") or 0)
        interes = min(interest_per_installment, pago)
        capital = max(0, pago - interes)

        fecha = "-"
        if p.get("date"):
            try:
                fecha = to_rd(p["date"]).strftime("%d/%m/%Y %I:%M %p")
            except Exception:
                fecha = "-"

        boton_eliminar = ""
        if user["role"] == "admin":
            boton_eliminar = f"""
            <form method="post"
                  action="/payment/delete/{p['id']}"
                  onsubmit="return confirm('¿Eliminar este pago y revertirlo?');"
                  style="display:inline;">
              <button type="submit"
              style="background:none;border:none;color:#dc2626;font-size:18px;cursor:pointer;">
                🗑️
              </button>
            </form>
            """

        history_rows += f"""
        <tr>
          <td>{i}</td>
          <td>{fmt_money(pago)}</td>
          <td>{fmt_money(capital)}</td>
          <td>{fmt_money(interes)}</td>
          <td>{fecha}</td>
          <td style="text-align:center;display:flex;gap:10px;justify-content:center;">
            <a target="_blank" href="/payment/{p['id']}/print">🖨</a>
            {boton_eliminar}
          </td>
        </tr>
        """
    # ===============================
    # FECHAS TXT (SIEMPRE DEFINIDAS)
    # ===============================
    fecha_inicio_txt = "-"
    fecha_fin_txt = "-"

    if start_date:
        try:
            fecha_inicio_txt = start_date.strftime("%d/%m/%Y")
        except Exception:
            fecha_inicio_txt = "-"

    if end_date:
        try:
            fecha_fin_txt = end_date.strftime("%d/%m/%Y")
        except Exception:
            fecha_fin_txt = "-"

	# ===============================
    # 📅 CALENDARIO COMPLETO DE PAGOS
    # ===============================
    calendario_html = ""

    if start_date:

        if frequency == "diario":
            delta = timedelta(days=1)
        elif frequency == "semanal":
            delta = timedelta(days=7)
        elif frequency == "quincenal":
            delta = timedelta(days=14)
        elif frequency == "mensual":
            delta = timedelta(days=30)
        else:
            delta = timedelta(days=7)

        for i in range(1, term_count + 1):

            fecha_cuota = start_date + (delta * i)

            estado_icono = "⏳"
            color = "#6b7280"

            if i <= cuotas_pagadas:
                estado_icono = "✅"
                color = "#16a34a"
            elif fecha_cuota < date.today():
                estado_icono = "🔴"
                color = "#dc2626"

            calendario_html += f"""
            <div style="
                padding:6px 0;
                font-weight:700;
                color:{color};
                display:flex;
                justify-content:space-between;
            ">
                <span>Cuota {i}</span>
                <span>{estado_icono} {fecha_cuota.strftime('%d/%m/%Y')}</span>
            </div>
            """


    # ===============================
    # HTML FINAL (MENÚ COMPLETO)
    # ===============================
    body = f"""
    <div class="card">
      <h2>📄 Préstamo #{loan_id}</h2>

      <p><strong>Cliente:</strong> {loan['first_name']} {loan['last_name']}</p>
      <p><strong>Teléfono:</strong> {phone if phone else "-"}</p>

      {alerta_html}

      <hr>

      <details open>
        <summary style="font-size:18px;font-weight:900;">📊 Resumen</summary>
        <p><strong>Capital aprobado:</strong> {fmt_money(amount)}</p>
        <p><strong>Descuento inicial ({upfront_percent}%):</strong> -{fmt_money(upfront_discount)}</p>
        <p><strong>Monto entregado:</strong> {fmt_money(amount_delivered)}</p>
        <p><strong>Interés total ({rate}%):</strong> {fmt_money(total_interest)}</p>
        <p><strong>Total a pagar:</strong> {fmt_money(total_to_pay)}</p>
      </details>

      <hr>

      <details open>
        <summary style="font-size:18px;font-weight:900;">💰 Pagos</summary>
        <p><strong>Pago por {pago_label}:</strong> {fmt_money(installment)}</p>

        <p>
          <strong>Cuotas:</strong> {cuotas_pagadas} de {term_count}<br>
          <span style="color:#16a34a;font-weight:bold;">
            {cuotas_pagadas} pagadas
          </span>
          •
          <span style="color:#dc2626;font-weight:bold;">
            {cuotas_restantes} restantes
          </span>
        </p>

		<hr>
        <h4 style="margin-top:12px;">📅 Calendario de pagos</h4>
        <div style="
            background:#f8fafc;
            padding:12px;
            border-radius:12px;
            margin-top:10px;
        ">
            {calendario_html}
        </div>

      <hr>

      <details>
        <summary style="font-size:18px;font-weight:900;">📅 Fechas</summary>
        <p><strong>Inicio:</strong> {fecha_inicio_txt}</p>
        <p><strong>Fin:</strong> {fecha_fin_txt}</p>
        <p><strong>Días restantes:</strong> {days_left}</p>
      </details>

      <hr>

      <details open>
        <summary style="font-size:18px;font-weight:900;">⏰ Siguiente pago</summary>

        <p><strong>Fecha:</strong>
          <span style="color:{color_fecha_pago};font-weight:900;">
            {proximo_pago_txt}
          </span>
        </p>

        <p><strong>Monto:</strong> {fmt_money(proximo_pago_monto)}</p>
        <p><strong>Estado:</strong> {proximo_pago_estado}</p>

        <div style="
  display:flex;
  justify-content:center;
  gap:14px;
  margin:22px 0;
  flex-wrap:wrap;
">

  <!-- BOTÓN WHATSAPP -->
  <a
    href="{whatsapp_recordatorio}"
    target="_blank"
    style="
      display:flex;
      align-items:center;
      gap:8px;
      background:linear-gradient(135deg,#16a34a,#22c55e);
      color:#ffffff;
      padding:14px 22px;
      border-radius:18px;
      font-weight:800;
      font-size:14px;
      text-decoration:none;
      box-shadow:0 10px 25px rgba(34,197,94,.45);
      transition:transform .15s ease, box-shadow .15s ease;
    "
    onmouseover="this.style.transform='scale(1.05)';this.style.boxShadow='0 14px 30px rgba(34,197,94,.6)'"
    onmouseout="this.style.transform='scale(1)';this.style.boxShadow='0 10px 25px rgba(34,197,94,.45)'"
  >
    📲 Recordar por WhatsApp
  </a>

  <!-- BOTÓN REGISTRAR PAGO -->
  <a
    href="/payment/new/{loan_id}"
    style="
      display:flex;
      align-items:center;
      gap:8px;
      background:linear-gradient(135deg,#0ea5e9,#2563eb);
      color:#ffffff;
      padding:14px 22px;
      border-radius:18px;
      font-weight:800;
      font-size:14px;
      text-decoration:none;
      box-shadow:0 10px 25px rgba(37,99,235,.45);
      transition:transform .15s ease, box-shadow .15s ease;
    "
    onmouseover="this.style.transform='scale(1.05)';this.style.boxShadow='0 14px 30px rgba(37,99,235,.6)'"
    onmouseout="this.style.transform='scale(1)';this.style.boxShadow='0 10px 25px rgba(37,99,235,.45)'"
  >
    ➕ Registrar pago
  </a>

</div>

      <hr>

      <h3>📜 Historial de pagos</h3>
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Monto</th>
            <th>Capital</th>
            <th>Interés</th>
            <th>Fecha</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {history_rows if history_rows else "<tr><td colspan='6'>Sin pagos</td></tr>"}
        </tbody>
      </table>

    </div>
    """

    cur.close()
    conn.close()

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )


from datetime import datetime, timedelta

@app.route("/payment/<int:payment_id>/print")
@login_required
def print_payment(payment_id):

    import pytz
    from flask import Response

    # 👇 IMPORTANTE
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # ===============================
    # LEER PAGO + PRÉSTAMO + CLIENTE + COBRADOR
    # ===============================
    cur.execute("""
        SELECT 
            p.amount,
            p.date,
            p.cuota_numero,
            l.term_count,
            l.amount AS capital_aprobado,
            l.rate,
            c.first_name,
            c.last_name,
            c.phone,
            p.collector_name,
            p.collector_phone,
            u.phone AS company_phone
        FROM payments p
        JOIN loans l ON l.id = p.loan_id
        JOIN clients c ON c.id = l.client_id
        LEFT JOIN users u ON u.id = l.created_by
        WHERE p.id = %s
    """, (payment_id,))

    p = cur.fetchone()
    if not p:
        cur.close()
        conn.close()
        return "Pago no encontrado", 404



    # ===============================
    # FECHA RD
    # ===============================
    fecha = p["date"]
    if not isinstance(fecha, datetime):
        fecha = datetime.combine(fecha, datetime.min.time())

    rd = pytz.timezone("America/Santo_Domingo")
    if fecha.tzinfo is None:
        fecha = pytz.utc.localize(fecha)

    fecha_txt = fecha.astimezone(rd).strftime("%d/%m/%Y %I:%M %p RD")

    # ===============================
    # CÁLCULOS
    # ===============================
    capital = float(p["capital_aprobado"])
    rate = float(p["rate"])
    term = int(p["term_count"])
    pago = float(p["amount"])

    interes_total = round(capital * rate / 100 * term, 2)
    total = round(capital + interes_total, 2)
    cuota = round(total / term, 2)

    interes_pago = round(interes_total / term, 2)
    capital_pago = round(pago - interes_pago, 2)

    cuotas_txt = f'{p["cuota_numero"]}/{p["term_count"]}'

    cobrador = p["collector_name"] or "N/A"
    tel_cobrador = p["collector_phone"] or "-"
    tel_empresa = "8495700819"

    # ===============================
    # HTML 58MM – LETRA GRANDE
    # ===============================
    html = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=220, initial-scale=1">
<title>Recibo</title>

<style>
body {{
  font-family: monospace;
  font-size: 18px;
  margin: 0;
  padding: 8px;
}}
.ticket {{ width: 220px; }}
.center {{ text-align: center; }}
.bold {{ font-weight: bold; }}
.big {{ font-size: 22px; }}
.line {{ border-top: 2px dashed #000; margin: 8px 0; }}
.row {{ display:flex; justify-content:space-between; margin:4px 0; }}
</style>
</head>

<body onload="window.print()">

<div class="ticket">

<div class="center bold big">JDM CASH NOW</div>
<div class="center bold">LA FACTORIA DEL POZO</div>
<div class="center">TEL EMPRESA: {tel_empresa}</div>

<div class="line"></div>

<div><b>Cliente:</b> {p["first_name"]} {p["last_name"]}</div>
<div><b>Teléfono:</b> {p["phone"]}</div>
<div><b>Fecha:</b> {fecha_txt}</div>

<div class="line"></div>

<div><b>Cobrador:</b> {cobrador}</div>
<div><b>Tel Cobrador:</b> {tel_cobrador}</div>

<div class="line"></div>

<div class="center bold">RESUMEN DEL PRESTAMO</div>

<div class="row"><span>Capital</span><span>RD$ {capital:,.2f}</span></div>
<div class="row"><span>Interés</span><span>RD$ {interes_total:,.2f}</span></div>
<div class="row bold"><span>TOTAL</span><span>RD$ {total:,.2f}</span></div>
<div class="row"><span>Cuota</span><span>RD$ {cuota:,.2f}</span></div>
<div class="row"><span>Cuotas</span><span>{cuotas_txt}</span></div>

<div class="line"></div>

<div class="center bold">PAGO REALIZADO</div>

<div class="row"><span>Monto</span><span>RD$ {pago:,.2f}</span></div>
<div class="row"><span>Capital</span><span>RD$ {capital_pago:,.2f}</span></div>
<div class="row"><span>Interés</span><span>RD$ {interes_pago:,.2f}</span></div>

<div class="line"></div>

<div class="center bold">GRACIAS POR SU PAGO</div>
<div class="center bold">AMEN</div>
<div class="center">JDM Cash Now</div>

</div>
</body>
</html>
"""

    cur.close()
    conn.close()

    return Response(html, mimetype="text/html")



@app.route("/payment/new/<int:loan_id>", methods=["GET", "POST"])
@login_required
def new_payment(loan_id):

    # ====================================================
    # RECIBIR SI VIENE DE ATRASOS
    # ====================================================
    installment_param = request.args.get("installment", type=int)
    late = request.args.get("late")
    is_late_payment = late == "1"

    user = current_user()

    # 👇 IMPORTANTE
    conn = get_conn()
    cur = conn.cursor()

    # ===============================
    # OBTENER PRÉSTAMO
    # ===============================
    cur.execute("SELECT * FROM loans WHERE id=%s", (loan_id,))
    loan = cur.fetchone()

    if not loan:
        flash("Préstamo no encontrado.", "danger")
        cur.close()
        conn.close()
        return redirect(url_for("loans"))

    remaining = float(loan["remaining"])
    amount_total = float(loan["amount"])
    rate = float(loan.get("rate") or 0)
    term_count = int(loan.get("term_count") or 1)

    total_to_pay = amount_total + (amount_total * rate / 100 * term_count)
    installment = total_to_pay / term_count if term_count else total_to_pay

	# ===============================
    # CALCULO REAL DE CUOTAS PAGADAS
    # ===============================
    cur.execute("""
        SELECT COALESCE(SUM(amount),0) AS total_pagado
        FROM payments
        WHERE loan_id=%s
    """, (loan_id,))

    row = cur.fetchone()
    total_pagado = float(row["total_pagado"] or 0)

    valor_cuota = float(installment)

    cuotas_pagadas = int(total_pagado // valor_cuota)
    cuotas_restantes = max(term_count - cuotas_pagadas, 0)

	# ===============================
    # CALCULAR SEMANAS ADELANTADAS
    # ===============================
    cur.execute("""
        SELECT COALESCE(SUM(weeks_advanced),0) AS adelantadas
        FROM payments
        WHERE loan_id=%s
    """, (loan_id,))

    row_adv = cur.fetchone()
    semanas_pendientes = int(row_adv["adelantadas"] or 0)

    # ===============================
    # AVISO SI HAY ADELANTOS (NO BLOQUEA)
    # ===============================
    if semanas_pendientes > 0 and request.method == "POST":
        flash(
            f"ℹ️ Aviso: el cliente tiene {semanas_pendientes} semana(s) adelantada(s). "
            f"El sistema permite registrar pagos adicionales como abono.",
            "info"
        )

    # ===============================
    # POST → GUARDAR PAGO
    # ===============================
    if request.method == "POST":

        payment_type = request.form.get("payment_type") or "normal"
        advance_unit = request.form.get("advance_unit")
        advance_count = request.form.get("advance_count", type=int) or 0

        # -------------------------------
        # CALCULAR MONTO Y SEMANAS
        # -------------------------------
        if payment_type == "advance":

            if advance_unit == "dias":
                weeks_advanced = max(1, advance_count // 7)
            elif advance_unit == "meses":
                weeks_advanced = advance_count * 4
            else:
                weeks_advanced = advance_count

            amount = installment * weeks_advanced
            note = f"Adelanto de {advance_count} {advance_unit}"

        else:
            weeks_advanced = 0

            # si es atraso usar cuota automática
            if is_late_payment:
                amount = installment
            else:
                amount = request.form.get("amount", type=float)

            note = "Pago normal"

        # ===============================
        # CALCULAR INTERÉS / CAPITAL
        # ===============================

        # Convertir monto de forma segura
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            amount = None

        # Solo validar monto si NO es adelanto
        if payment_type != "advance":
            if not amount or amount <= 0:
                flash("❌ Debes ingresar un monto mayor que 0.", "danger")
                cur.close()
                conn.close()
                return redirect(request.url)

        # Si es adelanto y por alguna razón amount quedó vacío
        if payment_type == "advance" and not amount:
            amount = installment * weeks_advanced

	    # ===============================
        # CALCULAR INTERÉS Y CAPITAL
        # ===============================

        interes_total = amount_total * rate / 100 * term_count
        interes_por_cuota = interes_total / term_count

        interest = round(interes_por_cuota, 2)
        capital = round(amount - interest, 2)

     
		

        # ===============================
        # COBRADOR
        # ===============================
        collector_name = user.get("username") or ""
        collector_phone = user.get("phone") or ""
        created_by = user["id"]

        # ====================================================
        # 🔢 DETERMINAR CUOTA A PAGAR
        # ====================================================

        if is_late_payment and installment_param:

            # validar que no esté pagada
            cur.execute("""
                SELECT 1 FROM payments
                WHERE loan_id=%s AND cuota_numero=%s
            """, (loan_id, installment_param))

            if cur.fetchone():
                flash("❌ Esta cuota ya está pagada.", "danger")
                cur.close()
                conn.close()
                return redirect(url_for("loan_detail", loan_id=loan_id))

            cuota_numero = installment_param
            note = f"Pago cuota atrasada #{cuota_numero}"

            # pago normal no adelanta semanas
            if payment_type != "advance":
                weeks_advanced = 0

        else:
            # siguiente cuota normal
            cur.execute("""
                SELECT COALESCE(MAX(cuota_numero), 0) AS pagados
                FROM payments
                WHERE loan_id = %s
            """, (loan_id,))

            pagados = cur.fetchone()["pagados"]
            cuota_numero = pagados + 1

            # evitar pagar más cuotas que el préstamo
            if cuota_numero > term_count:
                flash("❌ Este préstamo ya está completamente pagado.", "danger")
                cur.close()
                conn.close()
                return redirect(url_for("loan_detail", loan_id=loan_id))

            # pago normal no adelanta semanas
            if payment_type != "advance":
                weeks_advanced = 0

		# ===============================
        # INSERTAR PAGO + ACTUALIZAR PRÉSTAMO
        # ===============================

        print("---- DEBUG ----")
        print("payment_type:", payment_type)
        print("amount:", amount)
        print("weeks_advanced:", weeks_advanced)
        print("installment_param:", installment_param)
        print("----------------")

        try:

            print("PLACEHOLDER TEST")
            print("loan_id:", loan_id)
            print("amount:", amount)
            print("capital:", capital)
            print("interest:", interest)
            print("weeks_advanced:", weeks_advanced)
            print("note:", note)
            print("collector_name:", collector_name)
            print("collector_phone:", collector_phone)
            print("created_by:", created_by)
            print("cuota_numero:", cuota_numero)

            # ==========================================
            # 1️⃣ INSERTAR PAGO (GUARDA CUOTA)
            # ==========================================
            cur.execute("""
                INSERT INTO payments
                (loan_id, amount, capital, interest, date,
                 weeks_advanced, note,
                 collector_name, collector_phone, created_by,
                 cuota_numero)
                VALUES (%s,%s,%s,%s,NOW(),
                        %s,%s,
                        %s,%s,%s,
                        %s)
            """, (
                loan_id,
                amount,
                capital,
                interest,
                weeks_advanced,
                note,
                collector_name,
                collector_phone,
                created_by,
                cuota_numero
            ))

            # ==========================================
            # 2️⃣ DESCONTAR CAPITAL E INTERÉS DEL PRÉSTAMO
            # ==========================================
            if payment_type != "advance":

                total_descuento = capital + interest

                cur.execute("""
                    UPDATE loans
                    SET
                        remaining_capital = GREATEST(remaining_capital - %s, 0),
                        remaining = GREATEST(remaining - %s, 0),
                        status = CASE
                            WHEN remaining_capital - %s <= 0 THEN 'PAGADO'
                            ELSE status
                        END
                    WHERE id = %s
                """, (
                    capital,
                    total_descuento,
                    capital,
                    loan_id
                ))


            # ==========================================
            # 3️⃣ ACTUALIZAR PROXIMA FECHA DE COBRO
            # ==========================================

            if payment_type != "advance":

                cur.execute("""
                    UPDATE loans
                    SET next_payment_date =
                        start_date + (
                            (
                                SELECT COUNT(*) + 1 
                                FROM payments 
                                WHERE loan_id = %s
                            ) * 
                            CASE
                                WHEN frequency = 'semanal' THEN INTERVAL '7 day'
                                WHEN frequency = 'diario' THEN INTERVAL '1 day'
                                WHEN frequency = 'quincenal' THEN INTERVAL '14 day'
                                WHEN frequency = 'mensual' THEN INTERVAL '1 month'
                                ELSE INTERVAL '7 day'
                            END
                        )
                    WHERE id = %s
                """, (loan_id, loan_id))

            else:

                dias_extra = weeks_advanced * 7

                cur.execute("""
                    UPDATE loans
                    SET next_payment_date =
                        next_payment_date + (%s * INTERVAL '1 day')
                    WHERE id = %s
                """, (dias_extra, loan_id))
			
            # ==========================================
            # 4️⃣ SUMAR DINERO AL BANCO (CAJA)
            # ==========================================

            ruta_valor = "Cobro"

            cur.execute("""
                INSERT INTO cash_reports (
                    user_id,
                    movement_type,
                    amount,
                    ruta,
                    note,
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s, NOW())
            """, (
                user["id"],
                "pago_cliente",
                amount,
                ruta_valor,
                f"Pago recibido préstamo #{loan_id}"
            ))


            # ==========================================
            # 5️⃣ CONFIRMAR TODO
            # ==========================================
            conn.commit()


        except Exception as e:
            conn.rollback()

            print("🔥 ERROR SQL:", e)

            if "uq_payments_loan_cuota" in str(e):
                flash("❌ Esta cuota ya fue registrada.", "danger")
            else:
                flash(f"❌ Error guardando pago: {e}", "danger")

            cur.close()
            conn.close()
            return redirect(request.url)

        # ==========================================
        # 6️⃣ CERRAR CONEXIÓN
        # ==========================================
        cur.close()
        conn.close()

        flash("Pago registrado correctamente.", "success")
        return redirect(url_for("loan_detail", loan_id=loan_id))


    # ===============================
    # GET → FORMULARIO
    # ===============================
    body = f"""
    <div class="card">
      <h2>Registrar pago – Préstamo #{loan_id}</h2>

      <form method="post">

        <label>Tipo de pago</label>
        <select name="payment_type" id="ptype" onchange="toggleAdvance()" required>
          <option value="normal">Capital + interés</option>
          <option value="advance">⏩ Adelanto</option>
        </select>

        <div id="advanceBox" style="display:none;margin-top:8px;">

          <label>Unidad de adelanto</label>
          <select name="advance_unit" id="advance_unit" onchange="updateAdvanceLabel()">
            <option value="dias">Días</option>
            <option value="semanas" selected>Semanas</option>
            <option value="meses">Meses</option>
          </select>

          <label id="advance_label">Cantidad de semanas adelantadas</label>
          <input type="number" name="advance_count" min="1" value="1">

        </div>

        <label>Monto</label>
        <input type="number" step="0.01" name="amount" id="amountInput" required>

        <button type="submit" class="btn btn-primary">Guardar pago</button>

      </form>

      <p style="margin-top:10px;font-size:13px;">
        Saldo actual: <strong>{fmt_money(remaining)}</strong><br>
        Cuota: <strong>{fmt_money(installment)}</strong>
      </p>
    </div>

      <script>
      function toggleAdvance() {{
        const t = document.getElementById("ptype").value;
        const box = document.getElementById("advanceBox");
        const amountInput = document.getElementById("amountInput");

        box.style.display = t === "advance" ? "block" : "none";

        if (t === "advance") {{
          amountInput.removeAttribute("required");
          amountInput.value = "";
        }} else {{
          amountInput.setAttribute("required", "required");
        }}
      }}

      function updateAdvanceLabel() {{
        const unit = document.getElementById("advance_unit").value;
        const label = document.getElementById("advance_label");

        if (unit === "dias") {{
          label.innerText = "Cantidad de días adelantados";
        }} else if (unit === "meses") {{
          label.innerText = "Cantidad de meses adelantados";
        }} else {{
          label.innerText = "Cantidad de semanas adelantadas";
        }}
      }}

      document.addEventListener("DOMContentLoaded", function() {{
        toggleAdvance();
      }});
      </script>
      """

    cur.close()
    conn.close()

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )

# ============================================================
# 🗑️ ELIMINAR PAGO + REVERSIÓN REAL DE CAJA (FIX DEFINITIVO)
# ============================================================

@app.route("/payment/delete/<int:payment_id>", methods=["POST"])
@login_required
def delete_payment(payment_id):

    user = current_user()
    if user["role"] not in ("admin", "supervisor"):
        flash("⛔ No autorizado.", "danger")
        return redirect(url_for("dashboard"))

    conn = get_conn()
    cur = conn.cursor()

    try:
        # ===============================
        # 1️⃣ OBTENER PAGO
        # ===============================
        cur.execute("""
            SELECT id, loan_id, amount, interest
            FROM payments
            WHERE id = %s
        """, (payment_id,))
        pay = cur.fetchone()

        if not pay:
            raise Exception("Pago no encontrado")

        loan_id = pay["loan_id"]
        monto = pay["amount"]

        # ===============================
        # 2️⃣ REVERSAR CAJA (FIX REAL)
        # registra egreso NEGATIVO en caja
        # ===============================
        cur.execute("""
            INSERT INTO cash_reports (
                amount,
                movement_type,
                note,
                created_at,
                loan_id
            )
            VALUES (%s, 'EGRESO', %s, NOW(), %s)
        """, (
            -monto,  # ← NEGATIVO (CLAVE)
            f"Reversión eliminación pago #{payment_id}",
            loan_id
        ))

        # ===============================
        # 3️⃣ BORRAR EL PAGO
        # ===============================
        cur.execute("""
            DELETE FROM payments
            WHERE id = %s
        """, (payment_id,))

        if cur.rowcount == 0:
            raise Exception("El pago no se eliminó en la base de datos")

        # ===============================
        # 4️⃣ REACTIVAR PRÉSTAMO
        # ===============================
        cur.execute("""
            UPDATE loans
            SET status = 'ACTIVO'
            WHERE id = %s
        """, (loan_id,))

        # ===============================
        # 5️⃣ GUARDAR CAMBIOS
        # ===============================
        conn.commit()

        flash("✅ Pago eliminado y caja revertida correctamente.", "success")

    except Exception as e:
        conn.rollback()
        flash(f"❌ Error eliminando pago: {e}", "danger")

    finally:
        cur.close()
        conn.close()

    return redirect(url_for("loan_detail", loan_id=loan_id))

# ============================================================
# 🏦 BANCO – MENÚ PRINCIPAL (CONTROLADO POR ROL + COLORES)
# ============================================================
@app.route("/bank")
@login_required
def bank_home():
    user = current_user()

    # ===============================
    # 🔐 MENÚ SEGÚN ROL
    # ===============================
    if user["role"] == "cobrador":
        menu_html = """
        <a href="/bank/daily-list" class="bank-tile blue">📋 Lista diaria</a>
        <a href="/bank/expenses"   class="bank-tile red">🧾 Gastos</a>
        <a href="/bank/acta"       class="bank-tile yellow">💸 Descuento inicial</a>
        <a href="/bank/late"       class="bank-tile orange">⚠️ Atrasos</a>
        <a href="/bank/legal"      class="bank-tile purple">📜 Documento legal</a>
        <a href="/bank/advance"    class="bank-tile indigo">⏩ Adelantos</a>
        """
    else:
        menu_html = """
        <a href="/bank/daily-list" class="bank-tile blue">📋 Lista diaria</a>
        <a href="/bank/delivery"   class="bank-tile green">💰 Entrega</a>
        <a href="/bank/expenses"   class="bank-tile red">🧾 Gastos</a>
        <a href="/bank/acta"       class="bank-tile yellow">💸 Descuento inicial</a>
        <a href="/bank/routes"     class="bank-tile teal">🏦 Capital por ruta</a>
        <a href="/bank/advance"    class="bank-tile indigo">⏩ Adelantos</a>
        <a href="/bank/legal"      class="bank-tile purple">📜 Documento legal</a>
        <a href="/bank/late"       class="bank-tile orange">⚠️ Atrasos</a>
        """

    # ===============================
    # 🔴 BOTÓN BORRAR TODO (SOLO ADMIN)
    # ===============================
    admin_clear_button = ""
    if user["role"] == "admin":
        admin_clear_button = """
        <form method="post" action="/admin/clear-all"
              onsubmit="return confirm('⚠️ ESTO BORRARÁ TODO EL SISTEMA. ¿SEGURO?')"
              style="margin-top:22px;">
            <button type="submit" class="bank-tile danger" style="width:100%;">
                🗑️ BORRAR TODO EL SISTEMA
            </button>
        </form>
        """

    # ===============================
    # HTML + CSS
    # ===============================
    body = f"""
    <h2 style="text-align:center;margin-bottom:18px;">🏦 Banco</h2>

    <style>
      .bank-glass-wrap {{
        padding:14px;
        position:relative;
        z-index:10;
      }}

      .bank-menu {{
        display:grid;
        grid-template-columns:repeat(2,1fr);
        gap:14px;
      }}

      .bank-tile {{
        display:block;
        text-decoration:none;
        border-radius:22px;
        padding:20px 14px;
        text-align:center;
        font-weight:800;
        font-size:15px;
        color:#fff !important;
        border:none;
        cursor:pointer;
        box-shadow:0 14px 30px rgba(0,0,0,.28);
        transition:transform .12s ease;
      }}

      .bank-tile:active {{
        transform:scale(.96);
      }}

      /* 🎨 COLORES */
      .blue   {{ background:#4f8df7; }}
      .green  {{ background:#16a34a; }}
      .red    {{ background:#f87171; }}
      .yellow {{ background:#facc15; color:#111 !important; }}
      .teal   {{ background:#14b8a6; }}
      .indigo {{ background:#6366f1; }}
      .purple {{ background:#a855f7; }}
      .orange {{ background:#fb923c; }}
      .danger {{ background:#dc2626; }}
    </style>

    <div class="bank-glass-wrap">
      <div class="bank-menu">
        {menu_html}
      </div>

	  <a href="/bank/collector-map"
      style="width:100%;display:block;background:#009688;color:white;padding:18px;border-radius:16px;text-decoration:none;font-weight:bold;text-align:center;margin-top:15px;">
      📍 Ver ubicación cobrador
      </a>

      {admin_clear_button}
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )



# ============================================================
# 📜 DOCUMENTO LEGAL – MENÚ TIPO BANCO (AGUA / GLASS)
# ============================================================
@app.route("/bank/legal")
@login_required
def bank_legal():
    user = current_user()

    body = """
    <h2 style="text-align:center; margin-bottom:18px;">📜 Documento legal</h2>

    <style>
      .bank-glass-wrap { padding:14px; }
      .bank-menu {
        display:grid;
        grid-template-columns:repeat(2,1fr);
        gap:14px;
      }
      .bank-tile {
        text-decoration:none;
        border-radius:22px;
        padding:20px 14px;
        text-align:center;
        font-weight:700;
        font-size:15px;
        color:#111;
        background:rgba(255,255,255,0.22);
        backdrop-filter:blur(16px) saturate(160%);
        -webkit-backdrop-filter:blur(16px) saturate(160%);
        border:1px solid rgba(255,255,255,0.28);
        box-shadow:
          0 10px 24px rgba(0,0,0,0.18),
          inset 0 1px 0 rgba(255,255,255,0.35);
      }
      .bank-tile:active { transform:scale(0.96); }
    </style>

    <div class="bank-glass-wrap">
      <div class="bank-menu">

        <a href="/bank/legal/list" class="bank-tile">
          👁 Ver documentos legales
        </a>

      </div>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )

from flask import render_template_string, get_flashed_messages

# ============================================================
# 📄 DOCUMENTOS LEGALES – LISTA (POR ROL + FILTRO ADMIN)
# ============================================================
@app.route("/bank/legal/list")
@login_required
def bank_legal_list():
    user = current_user()
    conn = get_conn()
    cur = conn.cursor()

    collector_id = request.args.get("collector_id")

    # ===============================
    # OBTENER COBRADORES (SOLO ADMIN)
    # ===============================
    collectors = []
    if user["role"] == "admin":
        cur.execute("""
            SELECT id, username
            FROM users
            WHERE role = 'cobrador'
            ORDER BY username
        """)
        collectors = cur.fetchall()

    # ===============================
    # CONSULTA DE DOCUMENTOS
    # ===============================
    if user["role"] == "cobrador":
        # El cobrador SOLO ve sus documentos
        cur.execute("""
            SELECT l.id, l.amount, l.status,
                   c.first_name, c.last_name
            FROM loans l
            JOIN clients c ON c.id = l.client_id
            WHERE l.created_by = %s
            ORDER BY l.id DESC
        """, (user["id"],))

    else:
        # Admin / Supervisor
        if collector_id:
            cur.execute("""
                SELECT l.id, l.amount, l.status,
                       c.first_name, c.last_name
                FROM loans l
                JOIN clients c ON c.id = l.client_id
                WHERE l.created_by = %s
                ORDER BY l.id DESC
            """, (collector_id,))
        else:
            cur.execute("""
                SELECT l.id, l.amount, l.status,
                       c.first_name, c.last_name
                FROM loans l
                JOIN clients c ON c.id = l.client_id
                ORDER BY l.id DESC
            """)

    loans = cur.fetchall()
    cur.close()
    conn.close()

    # ===============================
    # HTML
    # ===============================
    html = """
    <h2 class="text-center mb-4">📄 Documentos legales</h2>

    {% if user.role == 'admin' %}
    <form method="get" style="max-width:320px; margin:0 auto 20px;">
      <select name="collector_id"
              onchange="this.form.submit()"
              class="btn btn-secondary"
              style="width:100%;">
        <option value="">🔎 Todos los cobradores</option>
        {% for c in collectors %}
          <option value="{{ c.id }}"
            {% if request.args.get('collector_id') == c.id|string %}selected{% endif %}>
            {{ c.username }}
          </option>
        {% endfor %}
      </select>
    </form>
    {% endif %}

    {% if loans %}
    <div class="bank-grid">
      {% for loan in loans %}
        <a href="/bank/legal/view/{{ loan.id }}" class="bank-card">
          <div class="bank-icon">📄</div>
          <div class="bank-text">
            <strong>{{ loan.first_name }} {{ loan.last_name }}</strong><br>
            RD$ {{ "%.2f"|format(loan.amount) }}<br>
            <small>Estado: {{ loan.status }}</small>
          </div>
        </a>
      {% endfor %}
    </div>
    {% else %}
      <p style="text-align:center; opacity:.7;">
        No hay documentos legales para mostrar.
      </p>
    {% endif %}

    <style>
    .bank-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 20px;
    }

    .bank-card {
      display: flex;
      align-items: center;
      gap: 15px;
      padding: 20px;
      border-radius: 20px;
      background: #e9fff3;
      text-decoration: none;
      color: #14532d;
      box-shadow: 0 8px 18px rgba(0,0,0,.08);
      transition: transform .15s ease, box-shadow .15s ease;
    }

    .bank-card:hover {
      transform: translateY(-3px);
      box-shadow: 0 12px 22px rgba(0,0,0,.15);
    }

    .bank-icon {
      font-size: 34px;
    }

    .bank-text {
      font-size: 15px;
    }
    </style>
    """

    rendered_body = render_template_string(
        html,
        loans=loans,
        collectors=collectors,
        user=user
    )

    return render_template_string(
        TPL_LAYOUT,
        body=rendered_body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )




from flask import Response


from psycopg2.extras import RealDictCursor

# ============================================================
# 📄 VER DOCUMENTO LEGAL + CÉDULA + FIRMA (TODO EN BD)
# ============================================================
@app.route("/bank/legal/view/<int:loan_id>", methods=["GET"])
@login_required
def view_legal_document(loan_id):
    ensure_legal_columns()

    user = current_user()
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # 🔐 Permisos cobrador
    if user["role"] == "cobrador":
        cur.execute("""
            SELECT id
            FROM loans
            WHERE id = %s AND created_by = %s
        """, (loan_id, user["id"]))
        if not cur.fetchone():
            cur.close()
            conn.close()
            flash("No autorizado.", "danger")
            return redirect(url_for("bank"))

    # 📄 Préstamo (SOLO LECTURA, SIN CÁLCULOS)
    cur.execute("""
        SELECT
            l.id,
            l.amount,
            l.upfront_discount,
            l.amount_delivered,
            l.total_interest,
            l.total_to_pay,
            l.start_date,
            l.id_front_b64,
            l.id_back_b64,
            l.signature_b64,
            c.first_name,
            c.last_name,
            c.document_id,
            u.username AS collector_name
        FROM loans l
        JOIN clients c ON c.id = l.client_id
        JOIN users u ON u.id = l.created_by
        WHERE l.id = %s
    """, (loan_id,))

    loan = cur.fetchone()

    if not loan:
        cur.close()
        conn.close()
        flash("Préstamo no encontrado.", "danger")
        return redirect(url_for("bank"))

    cur.close()
    conn.close()

    body = render_template_string("""
<style>
.id-grid {
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:16px;
  margin-top:12px;
  margin-bottom:22px;
}
.id-field { display:flex; flex-direction:column; }
.id-label { font-weight:700; font-size:13px; margin-bottom:4px; }
.id-box {
  border:1px solid #333;
  border-radius:4px;
  height:150px;
  background:#fff;
  display:flex;
  align-items:center;
  justify-content:center;
  overflow:hidden;
}
.id-box img { max-width:100%; max-height:100%; }
</style>

<div class="card p-4">

<a href="/bank/legal/list" class="btn btn-secondary mb-3">Volver</a>

<h2 class="text-center mb-4">Contrato de Préstamo</h2>

<p><b>Cliente:</b> {{ loan.first_name }} {{ loan.last_name }}</p>
<p><b>Cédula:</b> {{ loan.document_id or "—" }}</p>

<p><b>Capital aprobado:</b> RD$ {{ "%.2f"|format(loan.amount or 0) }}</p>

{% if loan.upfront_discount and loan.upfront_discount > 0 %}
<p><b>Descuento inicial:</b> RD$ {{ "%.2f"|format(loan.upfront_discount) }}</p>
{% endif %}

{% if loan.amount_delivered and loan.amount_delivered > 0 %}
<p><b>Monto entregado:</b> RD$ {{ "%.2f"|format(loan.amount_delivered) }}</p>
{% endif %}

{% if loan.total_interest and loan.total_interest > 0 %}
<p><b>Interés total:</b> RD$ {{ "%.2f"|format(loan.total_interest) }}</p>
{% endif %}

<p><b>Fecha inicio:</b> {{ loan.start_date }}</p>
<p><b>Cobrador:</b> {{ loan.collector_name }}</p>

<hr>
<h4>📜 Compromiso de Pago</h4>

<p style="text-align:justify; line-height:1.6; font-weight:500;">
El cliente <b>{{ loan.first_name }} {{ loan.last_name }}</b> reconoce haber
recibido el capital del préstamo y se compromete de manera expresa, voluntaria
e irrevocable a pagar la totalidad de la deuda a <b>JDM CASH NOW</b>,
incluyendo capital, intereses, cargos y penalidades aplicables, en los plazos
establecidos.
</p>

<p style="text-align:justify; line-height:1.6; font-weight:500;">
El incumplimiento de este compromiso autoriza a <b>JDM CASH NOW</b> a iniciar
las acciones legales correspondientes conforme a la ley vigente.
</p>

<hr>

<h4>Cédula del cliente</h4>

<div class="id-grid">

  <!-- CÉDULA FRENTE -->
  <div class="id-field">
    <div class="id-label">Cédula (Frente)</div>

    {% if loan.id_front_b64 and loan.id_front_b64|length > 10 %}
      <div class="id-box">
        <img src="data:image/jpeg;base64,{{ loan.id_front_b64 }}">
      </div>
    {% else %}
      <form method="post"
            action="/bank/legal/upload-id-front/{{ loan.id }}"
            enctype="multipart/form-data">
        <input type="file"
               name="id_front"
               accept="image/*"
               capture="environment"
               required
               style="display:none"
               onchange="this.form.submit()">

        <button type="button"
          onclick="this.previousElementSibling.click()"
          style="
            width:100%;
            background:linear-gradient(135deg,#64748b,#475569);
            color:#fff;
            padding:14px;
            border-radius:18px;
            font-weight:800;
            font-size:14px;
            border:none;
            box-shadow:0 10px 25px rgba(100,116,139,.45);
          ">
          📸 Subir cédula (frente)
        </button>
      </form>
    {% endif %}
  </div>

  <!-- CÉDULA ATRÁS -->
  <div class="id-field">
    <div class="id-label">Cédula (Parte de atrás)</div>

    {% if loan.id_back_b64 and loan.id_back_b64|length > 10 %}
      <div class="id-box">
        <img src="data:image/jpeg;base64,{{ loan.id_back_b64 }}">
      </div>
    {% else %}
      <form method="post"
            action="/bank/legal/upload-id-back/{{ loan.id }}"
            enctype="multipart/form-data">
        <input type="file"
               name="id_back"
               accept="image/*"
               capture="environment"
               required
               style="display:none"
               onchange="this.form.submit()">

        <button type="button"
          onclick="this.previousElementSibling.click()"
          style="
            width:100%;
            background:linear-gradient(135deg,#64748b,#475569);
            color:#fff;
            padding:14px;
            border-radius:18px;
            font-weight:800;
            font-size:14px;
            border:none;
            box-shadow:0 10px 25px rgba(100,116,139,.45);
          ">
          📸 Subir cédula (atrás)
        </button>
      </form>
    {% endif %}
  </div>

</div>

<hr>

<h4>Firma del cliente</h4>

{% if loan.signature_b64 and loan.signature_b64|length > 10 %}
  <div style="display:flex;justify-content:center;margin-bottom:18px;">
    <img src="data:image/png;base64,{{ loan.signature_b64 }}"
         style="width:280px;border:1px solid #e5e7eb;border-radius:12px;">
  </div>
{% else %}
  <div style="display:flex;justify-content:center;margin:18px 0;">
    <a href="/bank/legal/sign/{{ loan.id }}"
       style="
         background:linear-gradient(135deg,#0ea5e9,#2563eb);
         color:#fff;
         padding:14px 26px;
         border-radius:18px;
         font-weight:800;
         font-size:14px;
         text-decoration:none;
         box-shadow:0 10px 25px rgba(37,99,235,.45);
       ">
      ✍️ Firmar documento
    </a>
  </div>
{% endif %}

<hr>

<div style="display:flex;justify-content:center;margin:22px 0;">
  <button onclick="window.print()"
    style="
      background:linear-gradient(135deg,#16a34a,#22c55e);
      color:#fff;
      padding:14px 28px;
      border-radius:20px;
      font-weight:900;
      font-size:15px;
      border:none;
      box-shadow:0 12px 30px rgba(34,197,94,.5);
      cursor:pointer;
    ">
    🖨️ Imprimir contrato
  </button>
</div>

""", loan=loan)

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )



# ======================================================
# 📸 SUBIR CÉDULA A BD (ÚNICA RUTA)
# ======================================================
@app.route("/bank/legal/upload-id-front/<int:loan_id>", methods=["POST"])
@login_required
def upload_id_front(loan_id):
    import base64

    file = request.files.get("id_front")
    if not file:
        flash("Imagen frontal no recibida.", "danger")
        return redirect(url_for("view_legal_document", loan_id=loan_id))

    img_b64 = base64.b64encode(file.read()).decode("utf-8")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE loans
        SET id_front_b64 = %s
        WHERE id = %s
    """, (img_b64, loan_id))
    conn.commit()
    cur.close()
    conn.close()

    flash("Cédula frontal guardada.", "success")
    return redirect(url_for("view_legal_document", loan_id=loan_id))

@app.route("/bank/legal/upload-id-back/<int:loan_id>", methods=["POST"])
@login_required
def upload_id_back(loan_id):
    import base64

    file = request.files.get("id_back")
    if not file:
        flash("Imagen trasera no recibida.", "danger")
        return redirect(url_for("view_legal_document", loan_id=loan_id))

    img_b64 = base64.b64encode(file.read()).decode("utf-8")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE loans
        SET id_back_b64 = %s
        WHERE id = %s
    """, (img_b64, loan_id))
    conn.commit()
    cur.close()
    conn.close()

    flash("Cédula trasera guardada.", "success")
    return redirect(url_for("view_legal_document", loan_id=loan_id))



# ======================================================
# ✍️ GUARDAR FIRMA DIGITAL EN BD (ÚNICA RUTA)
# ======================================================
@app.route("/bank/legal/sign/<int:loan_id>", methods=["GET", "POST"])
@login_required
def sign_legal_document(loan_id):
    ensure_legal_columns()

    user = current_user()
    conn = get_conn()
    cur = conn.cursor()

    if user["role"] == "cobrador":
        cur.execute("""
            SELECT id FROM loans
            WHERE id = %s AND created_by = %s
        """, (loan_id, user["id"]))
        if not cur.fetchone():
            cur.close()
            conn.close()
            flash("No autorizado.", "danger")
            return redirect(url_for("bank"))

    if request.method == "POST":
        data_url = request.form.get("signature")

        if not data_url:
            flash("Firma inválida.", "danger")
            return redirect(request.url)

        if "," in data_url:
            data_url = data_url.split(",", 1)[1]

        cur.execute("""
            UPDATE loans
            SET signature_b64 = %s
            WHERE id = %s
        """, (data_url, loan_id))

        conn.commit()
        cur.close()
        conn.close()

        flash("Documento firmado correctamente.", "success")
        return redirect(url_for("view_legal_document", loan_id=loan_id))

    body = """
    <div class="card p-4">
      <h3 class="text-center mb-3">✍️ Firma del cliente</h3>

      <canvas id="canvas"
        style="border:1px solid #000;width:100%;height:220px;touch-action:none;">
      </canvas>

      <form method="POST" onsubmit="saveSignature()">
        <input type="hidden" name="signature" id="signature">
        <button type="submit" class="btn btn-success w-100 mt-3">
          Guardar firma
        </button>
        <button type="button"
          class="btn btn-secondary w-100 mt-2"
          onclick="clearCanvas()">
          Limpiar
        </button>
      </form>
    </div>

    <script>
      const canvas = document.getElementById("canvas");
      const ctx = canvas.getContext("2d");
      let drawing = false;

      function resizeCanvas() {
        canvas.width = canvas.offsetWidth;
        canvas.height = 220;
        ctx.lineWidth = 2;
        ctx.lineCap = "round";
        ctx.strokeStyle = "#000";
      }
      resizeCanvas();

      function getPos(e) {
        const r = canvas.getBoundingClientRect();
        if (e.touches) {
          return {
            x: e.touches[0].clientX - r.left,
            y: e.touches[0].clientY - r.top
          };
        }
        return { x: e.offsetX, y: e.offsetY };
      }

      function startDraw(e) {
        e.preventDefault();
        drawing = true;
        const p = getPos(e);
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
      }

      function draw(e) {
        if (!drawing) return;
        e.preventDefault();
        const p = getPos(e);
        ctx.lineTo(p.x, p.y);
        ctx.stroke();
      }

      function stopDraw() {
        drawing = false;
      }

      function saveSignature() {
        document.getElementById("signature").value =
          canvas.toDataURL("image/png");
      }

      function clearCanvas() {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.beginPath();
      }

      canvas.addEventListener("mousedown", startDraw);
      canvas.addEventListener("mousemove", draw);
      canvas.addEventListener("mouseup", stopDraw);
      canvas.addEventListener("mouseleave", stopDraw);

      canvas.addEventListener("touchstart", startDraw, { passive: false });
      canvas.addEventListener("touchmove", draw, { passive: false });
      canvas.addEventListener("touchend", stopDraw);
    </script>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )


# ============================================================
# ⏩ ADELANTOS (RUTA REAL – SIN 404) — FILTRADO POR ROL
# ============================================================
@app.route("/bank/advance")
@login_required
def bank_advance():
    user = current_user()
    conn = get_conn()
    cur = conn.cursor()

    loan_filter_sql = ""
    params = []

    if user["role"] == "cobrador":
        loan_filter_sql = "AND l.created_by = %s"
        params.append(user["id"])

    sql = f"""
SELECT
p.id,
p.loan_id,
p.amount,
p.weeks_advanced,
p.date,
c.first_name,
c.last_name
FROM payments p
JOIN loans l ON l.id = p.loan_id
JOIN clients c ON c.id = l.client_id
WHERE COALESCE(p.weeks_advanced, 0) > 0
{loan_filter_sql}
ORDER BY p.date DESC NULLS LAST, p.id DESC
"""
    cur.execute(sql, params)

    adelantos = cur.fetchall() or []

    rows = ""
    for a in adelantos:
        fecha = "-"
        if a.get("date"):
            try:
                fecha = a["date"].strftime("%d/%m/%Y %I:%M %p")
            except Exception:
                fecha = "-"

        rows += f"""
        <tr>
        <td>{a['loan_id']}</td>
        <td>{a['first_name']} {a['last_name']}</td>
        <td>{a.get('weeks_advanced')}</td>
        <td>{fmt_money(a.get('amount'))}</td>
        <td>{fecha}</td>

        <td>
        <form method="POST" action="/advance/delete/{a['id']}"
        onsubmit="return confirm('¿Eliminar este pago adelantado?')">
        <button class="btn-delete">🗑 Eliminar</button>
        </form>
        </td>

        </tr>
        """

    if not rows:
        rows = "<tr><td colspan='6'>No hay pagos adelantados</td></tr>"

    body = f"""
    <div class="card">
    <h2>⏩ Pagos adelantados</h2>

    <table>
    <thead>
    <tr>
    <th>Préstamo</th>
    <th>Cliente</th>
    <th>Semanas adelantadas</th>
    <th>Monto</th>
    <th>Fecha</th>
    <th>Acción</th>
    </tr>
    </thead>

    <tbody>
    {rows}
    </tbody>

    </table>
    </div>

    <style>
    .btn-delete{{
      background:#e74c3c;
      color:white;
      border:none;
      padding:6px 12px;
      border-radius:8px;
      cursor:pointer;
      font-weight:bold;
    }}

    .btn-delete:hover{{
      background:#c0392b;
    }}
    </style>
    """

    cur.close()
    conn.close()

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )


# ============================================================
# ➕ AGREGAR CAPITAL A LA RUTA (SOLO ADMIN + CLAVE)
# ============================================================

@app.route("/ruta/agregar-capital", methods=["POST"])
@login_required
def agregar_capital_ruta():

    user = current_user()

    if user["role"] not in ("admin", "supervisor"):
        flash("Acceso denegado", "danger")
        return redirect(url_for("dashboard"))

    clave = request.form.get("clave")
    monto = float(request.form.get("amount") or 0)

    # 🔐 CLAVE OFICIAL
    if clave != "0219":
        flash("❌ Clave incorrecta", "danger")
        return redirect(url_for("dashboard"))

    if monto <= 0:
        flash("❌ Monto inválido", "danger")
        return redirect(url_for("dashboard"))

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO cash_movements (amount, type, description)
        VALUES (%s, 'ADD_CAPITAL', 'Capital agregado a la ruta')
    """, (monto,))

    conn.commit()
    cur.close()
    conn.close()

    flash(f"✅ Capital agregado correctamente: RD$ {monto:,.2f}", "success")
    return redirect(url_for("dashboard"))


# ============================================================
#  AUDITORÍA
# ============================================================

@app.route("/audit")
@login_required
@admin_required
def audit():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT a.id, a.created_at, a.action, a.detail, u.username
        FROM audit_log a
        LEFT JOIN users u ON u.id = a.user_id
        ORDER BY a.id DESC
        LIMIT 200;
    """)
    rows = cur.fetchall()

    cur.close()
    conn.close()

    rows_html = "".join([
        f"""
        <tr>
          <td>{r['id']}</td>
          <td>{r['created_at'].astimezone(RD_TZ).strftime("%Y-%m-%d %I:%M:%S %p")}</td>
          <td>{r['username'] or ''}</td>
          <td>{r['action']}</td>
          <td>{r['detail'] or ''}</td>
        </tr>
        """
        for r in rows
    ])

    body = f"""
    <div class="card">
      <h2>Auditoría</h2>
      <div class="table-wrapper">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Fecha</th>
              <th>Usuario</th>
              <th>Acción</th>
              <th>Detalle</th>
            </tr>
          </thead>
          <tbody>
            {rows_html}
          </tbody>
        </table>
      </div>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=current_user(),
        app_brand=APP_BRAND,
        admin_whatsapp=ADMIN_WHATSAPP,
        flashes=get_flashed_messages(with_categories=True),
        theme=get_theme()
    )
    
# ============================================================
#  REPORTES FINANCIEROS
# ============================================================

@app.route("/reportes", methods=["GET", "POST"])
@login_required
def reportes():
    user = current_user()

    if user["role"] not in ("admin", "supervisor"):
        flash("No tienes permiso para ver reportes.", "danger")
        return redirect(url_for("index"))

    desde = request.form.get("desde")
    hasta = request.form.get("hasta")
    tipo = request.form.get("tipo", "total")
    cobrador = request.form.get("cobrador")

    rows = []
    total = 0

    conn = get_conn()
    cur = conn.cursor()

    # ===============================
    # COBRADORES DISPONIBLES
    # ===============================
    cur.execute("""
        SELECT DISTINCT collector_name
        FROM payments
        WHERE collector_name IS NOT NULL
        ORDER BY collector_name
    """)
    cobradores = [r["collector_name"] for r in cur.fetchall()]

    if desde and hasta:

        params = [desde, hasta]
        filtro_cobrador = ""

        if cobrador:
            filtro_cobrador = "AND p.collector_name = %s"
            params.append(cobrador)

        if tipo == "interes":
            query = f"""
                SELECT p.date, p.loan_id, p.interest AS monto, p.collector_name
                FROM payments p
                WHERE p.date >= %s
                  AND p.date < (%s::date + INTERVAL '1 day')
                  AND p.interest > 0
                  {filtro_cobrador}
                ORDER BY p.date
            """
        elif tipo == "capital":
            query = f"""
                SELECT p.date, p.loan_id, p.capital AS monto, p.collector_name
                FROM payments p
                WHERE p.date >= %s
                  AND p.date < (%s::date + INTERVAL '1 day')
                  AND p.capital > 0
                  {filtro_cobrador}
                ORDER BY p.date
            """
        else:
            query = f"""
                SELECT p.date, p.loan_id, p.amount AS monto, p.collector_name
                FROM payments p
                WHERE p.date >= %s
                  AND p.date < (%s::date + INTERVAL '1 day')
                  {filtro_cobrador}
                ORDER BY p.date
            """

        cur.execute(query, tuple(params))
        rows = cur.fetchall()
        total = sum(r["monto"] for r in rows)

    cur.close()
    conn.close()

    # ===============================
    # HTML
    # ===============================
    rows_html = "".join([
        f"""
        <tr>
          <td>{r['date'].strftime('%Y-%m-%d %H:%M')}</td>
          <td>#{r['loan_id']}</td>
          <td>{r['collector_name']}</td>
          <td>{fmt_money(r['monto'])}</td>
        </tr>
        """
        for r in rows
    ]) or "<tr><td colspan='4'>Sin resultados</td></tr>"

    cobrador_opts = "".join([
        f"<option value='{c}' {'selected' if c==cobrador else ''}>{c}</option>"
        for c in cobradores
    ])

    body = f"""
    <div class="card">

      <div style="display:flex; gap:10px; margin-bottom:20px;">
        <a class="btn btn-secondary" href="{url_for('reportes')}">📊 General</a>
        <a class="btn btn-secondary" href="{url_for('reportes_cobradores')}">👥 Por cobrador</a>
        <a class="btn btn-secondary" href="{url_for('dashboard')}">📈 Dashboard</a>
      </div>

      <h2>📊 Reportes financieros</h2>

      <form method="post" style="display:flex; gap:10px; flex-wrap:wrap;">
        <input type="date" name="desde" value="{desde or ''}" required>
        <input type="date" name="hasta" value="{hasta or ''}" required>

        <select name="tipo">
          <option value="interes" {"selected" if tipo=="interes" else ""}>💰 Interés ganado</option>
          <option value="capital" {"selected" if tipo=="capital" else ""}>💵 Capital abonado</option>
          <option value="total" {"selected" if tipo not in ("interes","capital") else ""}>📈 Total cobrado</option>
        </select>

        <select name="cobrador">
          <option value="">👤 Todos los cobradores</option>
          {cobrador_opts}
        </select>

        <button class="btn btn-success">Ver reporte</button>
      </form>
    </div>

    <div class="card">
      <table>
        <thead>
          <tr>
            <th>Fecha</th>
            <th>Préstamo</th>
            <th>Cobrador</th>
            <th>Monto</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>

      <h3 style="text-align:right;margin-top:12px;">
        Total: {fmt_money(total)}
      </h3>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        app_brand=APP_BRAND,
        admin_whatsapp=ADMIN_WHATSAPP,
        flashes=get_flashed_messages(with_categories=True),
        theme=get_theme()
    )


# ============================================================
#  RESUMEN POR COBRADOR
# ============================================================

@app.route("/reportes/cobradores", methods=["GET", "POST"])
@login_required
def reportes_cobradores():
    user = current_user()

    if user["role"] not in ("admin", "supervisor"):
        flash("No tienes permiso.", "danger")
        return redirect(url_for("index"))

    desde = request.form.get("desde")
    hasta = request.form.get("hasta")

    rows = []

    conn = get_conn()
    cur = conn.cursor()

    if desde and hasta:
        cur.execute("""
            SELECT
                collector_name AS cobrador,
                COUNT(*) AS pagos,
                SUM(amount) AS total_cobrado,
                SUM(capital) AS total_capital,
                SUM(interest) AS total_interes
            FROM payments
            WHERE date >= %s
              AND date < (%s::date + INTERVAL '1 day')
              AND collector_name IS NOT NULL
            GROUP BY collector_name
            ORDER BY total_cobrado DESC
        """, (desde, hasta))

        rows = cur.fetchall()

    cur.close()
    conn.close()

    rows_html = "".join([
        f"""
        <tr>
          <td>{r['cobrador']}</td>
          <td>{r['pagos']}</td>
          <td>{fmt_money(r['total_capital'])}</td>
          <td>{fmt_money(r['total_interes'])}</td>
          <td><strong>{fmt_money(r['total_cobrado'])}</strong></td>
        </tr>
        """
        for r in rows
    ]) or "<tr><td colspan='5'>Sin resultados</td></tr>"

    body = f"""
    <div class="card">
      <h2>👥 Resumen por cobrador</h2>

      <form method="post" style="display:flex;gap:10px;flex-wrap:wrap;">
        <input type="date" name="desde" required>
        <input type="date" name="hasta" required>
        <button class="btn btn-success">Ver resumen</button>
      </form>
    </div>

    <div class="card">
      <table>
        <thead>
          <tr>
            <th>Cobrador</th>
            <th>Pagos</th>
            <th>Capital</th>
            <th>Interés</th>
            <th>Total</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        app_brand=APP_BRAND,
        admin_whatsapp=ADMIN_WHATSAPP,
        flashes=get_flashed_messages(with_categories=True),
        theme=get_theme()
    )

    # ==================================================
    # 📊 GRÁFICO ÚLTIMOS 7 DÍAS (GLOBAL)
    # ==================================================
    cur.execute("""
        SELECT
            DATE(p.date) AS dia,
            COALESCE(SUM(p.amount),0)   AS total,
            COALESCE(SUM(p.capital),0)  AS capital,
            COALESCE(SUM(p.interest),0) AS interes
        FROM payments p
        WHERE DATE(p.date) >= CURRENT_DATE - INTERVAL '7 days'
        GROUP BY DATE(p.date)
        ORDER BY dia
    """)
    rows = cur.fetchall()

    labels = [r["dia"].strftime("%Y-%m-%d") for r in rows]
    totals = [float(r["total"] or 0) for r in rows]
    capital_chart = [float(r["capital"] or 0) for r in rows]
    interes_chart = [float(r["interes"] or 0) for r in rows]

    # ==================================================
    # 💰 MÉTRICAS FINANCIERAS (GLOBAL)
    # ==================================================

    # 🟢 Capital invertido
    cur.execute("""
        SELECT COALESCE(SUM(amount),0)
        FROM cash_movements
        WHERE type IN ('INITIAL_CAPITAL','ADD_CAPITAL')
    """)
    capital_total = float(cur.fetchone()[0] or 0)

    # 🚶 TOTAL REAL EN LA CALLE (CAPITAL + INTERÉS CONTRATADO)
    cur.execute("""
        SELECT COALESCE(SUM(amount + total_interest_contract),0)
        FROM loans
        WHERE UPPER(status) = 'ACTIVO'
    """)
    capital_calle = float(cur.fetchone()[0] or 0)

    # 💸 Gastos
    cur.execute("""
        SELECT COALESCE(SUM(amount),0)
        FROM expenses
    """)
    gastos = float(cur.fetchone()[0] or 0)

    # 📈 Interés ganado
    cur.execute("""
        SELECT COALESCE(SUM(interest),0)
        FROM payments
    """)
    interes_ganado = float(cur.fetchone()[0] or 0)

    # 🏦 Caja disponible
    capital_caja = capital_total - capital_calle - gastos

    # 💎 Total real del negocio
    total_real = capital_total + interes_ganado - gastos

    cur.close()
    conn.close()

    # ==================================================
    # 🎨 RENDER
    # ==================================================
    body = f"""
    <div class="grid grid-2">

      <div class="card highlight">
        <h4>💰 Total real del negocio</h4>
        <h2>RD$ {total_real:,.2f}</h2>
        <small>Capital + interés − gastos</small>
      </div>

      <div class="card">
        <h4>🚶 Total en la calle</h4>
        <h2>RD$ {capital_calle:,.2f}</h2>
      </div>

      <div class="card">
        <h4>🏦 En caja</h4>
        <h2>RD$ {capital_caja:,.2f}</h2>
      </div>

      <div class="card">
        <h4>📈 Interés ganado</h4>
        <h2>RD$ {interes_ganado:,.2f}</h2>
      </div>

      <div class="card">
        <h4>💸 Gastos</h4>
        <h2>RD$ {gastos:,.2f}</h2>
      </div>

    </div>

    <div class="card">
      <h2>📈 Últimos 7 días</h2>
      <canvas id="chart" height="120"></canvas>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
    new Chart(document.getElementById('chart'), {{
        type: 'line',
        data: {{
            labels: {labels},
            datasets: [
                {{ label:'Total cobrado', data:{totals}, borderWidth:2 }},
                {{ label:'Capital', data:{capital_chart}, borderWidth:2 }},
                {{ label:'Interés', data:{interes_chart}, borderWidth:2 }}
            ]
        }}
    }});
    </script>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        app_brand=APP_BRAND,
        admin_whatsapp=ADMIN_WHATSAPP,
        flashes=get_flashed_messages(with_categories=True),
        theme=get_theme()
    )



# ============================================================
#  RESUMEN TOTAL DE LA RUTA (BLINDADO)
# ============================================================

@app.route("/ruta/resumen")
@login_required
def ruta_resumen():
    conn = get_conn()
    cur = conn.cursor()

    # 🟢 Capital total invertido
    cur.execute("""
        SELECT COALESCE(SUM(amount),0) AS capital_total
        FROM cash_movements
        WHERE type IN ('INITIAL_CAPITAL','ADD_CAPITAL')
    """)
    capital_total = float(cur.fetchone()["capital_total"] or 0)

    # 🚶 Capital en la calle (REAL)
    cur.execute("""
        SELECT COALESCE(SUM(remaining),0) AS capital_calle
        FROM loans
        WHERE UPPER(status) = 'ACTIVO'
    """)
    capital_calle = float(cur.fetchone()["capital_calle"] or 0)

    # 💸 Gastos
    cur.execute("""
        SELECT COALESCE(SUM(amount),0) AS gastos
        FROM expenses
    """)
    gastos = float(cur.fetchone()["gastos"] or 0)

    # 🏦 Capital en caja
    capital_caja = capital_total - capital_calle - gastos

    # 📈 Interés ganado
    cur.execute("""
        SELECT COALESCE(SUM(interest),0) AS interes
        FROM payments
    """)
    interes = float(cur.fetchone()["interes"] or 0)

    # 🔒 Total real (blindado)
    total_real = capital_total + interes - gastos

    cur.close()
    conn.close()

    body = f"""
    <div class="card">
      <h2>📊 Resumen Total de la Ruta</h2>

      <p>🚶 Capital en la calle: <b>{fmt_money(capital_calle)}</b></p>
      <p>🏦 En caja: <b>{fmt_money(capital_caja)}</b></p>
      <p>📈 Interés ganado: <b>{fmt_money(interes)}</b></p>
      <p>💸 Gastos: <b>{fmt_money(gastos)}</b></p>

      <hr>
      <h3>💰 TOTAL REAL: {fmt_money(total_real)}</h3>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=current_user(),
        app_brand=APP_BRAND,
        admin_whatsapp=ADMIN_WHATSAPP,
        flashes=get_flashed_messages(with_categories=True),
        theme=get_theme()
    )
# ============================================================
#  PRÉSTAMOS PAGADOS (FILTRADO POR ROL)
# ============================================================
@app.route("/prestamos/pagados")
@login_required
def prestamos_pagados():
    user = current_user()

    conn = get_conn()
    cur = conn.cursor()

    # ===============================
    # 🔐 FILTRO POR ROL
    # ===============================
    loan_filter_sql = ""
    pay_filter_sql = ""
    params = []

    if user["role"] == "cobrador":
        loan_filter_sql = "AND l.created_by = %s"
        pay_filter_sql  = "AND l.created_by = %s"
        params.append(user["id"])

    # ===============================
    # 📄 LISTA DE PRÉSTAMOS PAGADOS
    # ===============================
    cur.execute(f"""
        SELECT
            l.id,
            cl.first_name || ' ' || COALESCE(cl.last_name,'') AS customer_name,
            COALESCE(SUM(p.capital),0)  AS capital_pagado,
            COALESCE(SUM(p.interest),0) AS interes_pagado
        FROM loans l
        JOIN clients cl ON cl.id = l.client_id
        LEFT JOIN payments p ON p.loan_id = l.id
        WHERE UPPER(l.status) = 'PAGADO'
        {loan_filter_sql}
        GROUP BY l.id, cl.first_name, cl.last_name
        ORDER BY l.id DESC
    """, params)

    loans = cur.fetchall()

    # ===============================
    # 💰 TOTALES
    # ===============================
    cur.execute(f"""
        SELECT
            COALESCE(SUM(p.capital),0)  AS capital,
            COALESCE(SUM(p.interest),0) AS interes
        FROM payments p
        JOIN loans l ON l.id = p.loan_id
        WHERE UPPER(l.status) = 'PAGADO'
        {pay_filter_sql}
    """, params)

    row = cur.fetchone()
    capital_cobrado = float(row["capital"] or 0)
    interes_cobrado = float(row["interes"] or 0)
    total_cobrado   = capital_cobrado + interes_cobrado

    cur.close()
    conn.close()

    # ===============================
    # 🎨 HTML
    # ===============================
    body = f"""
    <h2>📄 Préstamos Pagados</h2>

    <div class="kpi-grid">
      <div class="kpi-card">
        <h4>Capital cobrado</h4>
        <div class="value">{fmt_money(capital_cobrado)}</div>
      </div>
      <div class="kpi-card">
        <h4>Interés ganado</h4>
        <div class="value">{fmt_money(interes_cobrado)}</div>
      </div>
      <div class="kpi-card">
        <h4>🔥 Total cobrado</h4>
        <div class="value">{fmt_money(total_cobrado)}</div>
      </div>
    </div>

    <table class="table">
      <tr>
        <th>Cliente</th>
        <th>Capital</th>
        <th>Interés</th>
        <th>Total</th>
      </tr>
      {''.join(f'''
      <tr>
        <td>{l["customer_name"]}</td>
        <td>{fmt_money(l["capital_pagado"])}</td>
        <td>{fmt_money(l["interes_pagado"])}</td>
        <td>{fmt_money(l["capital_pagado"] + l["interes_pagado"])}</td>
      </tr>
      ''' for l in loans)}
    </table>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        app_brand=APP_BRAND,
        theme=get_theme()
    )





# ============================================================
# 🗑️ BORRAR TODO EL SISTEMA (ADMIN + CLAVE 0219)
# ============================================================

SYSTEM_DELETE_PASSWORD = "0219"

@app.route("/admin/clear-all", methods=["GET", "POST"])
@login_required
def admin_clear_all():
    user = current_user()

    # 🔐 SOLO ADMIN
    if user["role"] != "admin":
        flash("⛔ No autorizado.", "danger")
        return redirect(url_for("bank_home"))

    if request.method == "POST":
        password = request.form.get("password", "").strip()
        confirm  = request.form.get("confirm", "").strip()

        if password != SYSTEM_DELETE_PASSWORD or confirm != "BORRAR":
            flash("⛔ Clave incorrecta o confirmación inválida.", "danger")
            return redirect(request.url)

        conn = get_conn()
        cur = conn.cursor()

        try:
            # 🔥 ORDEN CORRECTO (HIJOS → PADRES)
            cur.execute("DELETE FROM audit_log;")
            cur.execute("DELETE FROM payments;")
            cur.execute("DELETE FROM cash_reports;")
            cur.execute("DELETE FROM loans;")
            cur.execute("DELETE FROM clients;")

            # 🚫 NO BORRAR ADMINS
            cur.execute("""
                DELETE FROM users
                WHERE role != 'admin';
            """)

            conn.commit()
            flash("🗑️ SISTEMA BORRADO COMPLETAMENTE (admins intactos).", "success")

        except Exception as e:
            conn.rollback()
            flash(f"❌ Error crítico: {e}", "danger")

        finally:
            cur.close()
            conn.close()

        return redirect(url_for("bank_home"))

    # ===============================
    # FORMULARIO CONFIRMACIÓN
    # ===============================
    body = """
    <div class="card" style="max-width:520px;margin:auto;">
      <h2 style="color:#b91c1c;">🗑️ BORRAR TODO EL SISTEMA</h2>

      <p style="font-weight:600;color:#7f1d1d;">
        ⚠️ Esta acción es <b>IRREVERSIBLE</b>.<br>
        Se eliminarán TODOS los datos del sistema.
      </p>

      <form method="POST">
        <label>Clave de seguridad</label>
        <input type="password" name="password" class="form-control" required>

        <label style="margin-top:10px;">
          Escriba <b>BORRAR</b> para confirmar
        </label>
        <input type="text" name="confirm" class="form-control" required>

        <button class="btn btn-danger mt-3" style="width:100%;">
          🚨 BORRAR TODO DEFINITIVAMENTE
        </button>
      </form>

      <a href="/bank" class="btn btn-secondary mt-3" style="width:100%;">
        Cancelar
      </a>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )



    # ===============================
    # BODY HTML (TODO DENTRO DEL STRING)
    # ===============================
    body = f"""
    <div class="card">
      <h3>🏦 BANCO</h3>

      <div class="bank-menu" style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px">
        <a class="btn btn-secondary" href="/bank/daily-list">📋 Lista diaria</a>
        <a class="btn btn-primary"   href="/bank/delivery">💰 Entrega</a>
        <a class="btn btn-secondary" href="/bank/expenses">🧾 Gastos</a>
        <a class="btn btn-secondary" href="/bank/actas">📄 Acta</a>
        <a class="btn btn-primary"   href="/bank/routes">🏦 Banco (capital ruta)</a>
        <a class="btn btn-secondary" href="/bank/advance">⏩ Adelantos</a>
        <a class="btn btn-warning"   href="/bank/late">⚠️ Atrasos</a>
      </div>
    </div>

    {admin_clear_button}

    <style>
      /* ==============================
         📱 MODO TELÉFONO – MENÚ BANCO
         ============================== */
      @media (max-width: 768px) {{
        .bank-menu {{
          display: grid !important;
          grid-template-columns: repeat(2, 1fr);
          gap: 10px;
        }}

        .bank-menu a {{
          width: 100%;
          font-size: 14px;
          padding: 12px 10px;
          border-radius: 14px;
          text-align: center;
          white-space: normal;
        }}
      }}
    </style>
    """

    # ===============================
    # RENDER FINAL (UN SOLO RETURN)
    # ===============================
    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )


from datetime import date

@app.route("/bank/daily-list", methods=["GET", "POST"])
@login_required
def bank_daily_list():
    user = current_user()
    conn = get_conn()
    cur = conn.cursor()

    # ==================================================
    # ⛔ MOSTRAR LISTA SOLO LOS SÁBADOS  (AGREGADO)
    # ==================================================
    # weekday(): lunes=0 ... sábado=5
    if date.today().weekday() != 5:
        cur.close()
        conn.close()

        return render_template_string(
            TPL_LAYOUT,
            body="""
            <div class="card" style="text-align:center;padding:30px;">
                <h3>📅 Hoy no es sábado</h3>
                <p>La lista de cobro solo está disponible los sábados.</p>
            </div>
            """,
            user=user,
            flashes=get_flashed_messages(with_categories=True),
            admin_whatsapp=ADMIN_WHATSAPP,
            app_brand=APP_BRAND,
            theme=get_theme()
        )

    # ===============================
    # FILTRO POR COBRADOR (ORIGINAL)
    # ===============================
    params = []
    user_filter = ""
    filter_html = ""

    if user["role"] == "cobrador":
        user_filter = "AND l.created_by = %s"
        params.append(user["id"])
    else:
        filter_user = request.args.get("filter_user", type=int)
        if filter_user:
            user_filter = "AND l.created_by = %s"
            params.append(filter_user)

        cur.execute("""
            SELECT id, username
            FROM users
            WHERE role='cobrador'
            ORDER BY username
        """)
        cobradores = cur.fetchall()

        opts = "<option value=''>-- TODOS --</option>"
        for c in cobradores:
            sel = "selected" if filter_user == c["id"] else ""
            opts += f"<option value='{c['id']}' {sel}>{c['username']}</option>"

        filter_html = f"""
        <div style="margin:10px 0;">
          <b>Ver por cobrador:</b>
          <select onchange="location.href='/bank/daily-list'+(this.value?'?filter_user='+this.value:'')">
            {opts}
          </select>
        </div>
        """

    # ===============================
    # MARCAR COBRADO (ORIGINAL)
    # ===============================
    if request.method == "POST" and request.form.get("loan_id"):
        loan_id = request.form.get("loan_id", type=int)

        confirm_pay = request.form.get("confirm_pay")
        if confirm_pay != "yes":
            flash("⚠️ Debe confirmar el pago.", "warning")
        else:
            cur.execute("""
                SELECT 1
                FROM payments
                WHERE loan_id=%s
                  AND DATE(date)=CURRENT_DATE
                  AND status <> 'ANULADO'
                LIMIT 1
            """, (loan_id,))
            if cur.fetchone():
                flash("⚠️ Este préstamo ya fue cobrado hoy.", "warning")
            else:
                cur.execute("""
                    SELECT remaining, amount, rate, term_count
                    FROM loans
                    WHERE id=%s
                      AND status ILIKE 'activo'
                """, (loan_id,))
                loan = cur.fetchone()

                if not loan:
                    flash("❌ Préstamo no válido.", "danger")
                else:
                    capital = loan["amount"] / max(loan["term_count"] or 1, 1)
                    interes = loan["amount"] * (loan["rate"] or 0) / 100
                    total_pago = capital + interes

                    if loan["remaining"] < capital:
                        flash("🔒 Cliente adelantado. No se puede cobrar.", "warning")
                    else:
                        cur.execute("""
                            INSERT INTO payments (loan_id, amount, date, created_by)
                            VALUES (%s, %s, NOW(), %s)
                        """, (loan_id, total_pago, user["id"]))

                        new_remaining = loan["remaining"] - capital
                        status = "cerrado" if new_remaining <= 0 else "activo"

                        cur.execute("""
                            UPDATE loans
                            SET remaining=%s, status=%s
                            WHERE id=%s
                        """, (max(new_remaining, 0), status, loan_id))

                        conn.commit()
                        flash("✔ Pago registrado correctamente.", "success")

    # ==================================================
    # LISTA: SOLO PRÉSTAMOS QUE TOCAN HOY (AGREGADO)
    # ==================================================
    cur.execute(f"""
        SELECT
            l.id AS loan_id,
            c.first_name,
            c.last_name,
            c.phone,
            c.route,
            u.username AS cobrador,
            l.amount,
            l.rate,
            l.term_count,
            (
                SELECT COUNT(*)
                FROM payments p
                WHERE p.loan_id = l.id
                  AND DATE(p.date) = CURRENT_DATE
                  AND p.status <> 'ANULADO'
            ) AS pagado_hoy
        FROM loans l
        JOIN clients c ON c.id = l.client_id
        JOIN users u ON u.id = l.created_by
        WHERE l.status ILIKE 'ACTIVO'
          AND l.remaining > 0
          AND l.next_payment_date = CURRENT_DATE   -- 🔹 SOLO LOS QUE TOCAN HOY
          AND NOT EXISTS (                          -- 🔹 EXCLUIR ADELANTADOS
              SELECT 1
              FROM payments p
              WHERE p.loan_id = l.id
                AND p.weeks_advanced > 0
                AND p.status <> 'ANULADO'
          )
          {user_filter}
        ORDER BY c.route, c.first_name
    """, params)

    rows = cur.fetchall() or []

    # ===============================
    # CONSTRUIR HTML (ORIGINAL)
    # ===============================
    body = build_mobile_rows(rows, fmt_money, user, filter_html)

    cur.close()
    conn.close()

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )



# ============================================================
# CONSTRUCTOR HTML MOBILE / DESKTOP
# ============================================================
def build_mobile_rows(rows, fmt_money, user, filter_html=""):
    mobile_css = """
    <style>
    .mobile-list { display:none; }
    .mobile-card {
      background:#fff;
      border-radius:16px;
      padding:14px;
      margin-bottom:14px;
      box-shadow:0 6px 18px rgba(0,0,0,.08);
    }
    .mobile-name { font-size:18px; font-weight:700; }
    .mobile-phone { color:#666; margin:4px 0 8px; }
    .mobile-total { font-size:20px; font-weight:700; margin-bottom:10px; }
    @media (max-width:768px){
      table{display:none;}
      .mobile-list{display:block;}
    }
    </style>
    """

    mobile_rows = ""
    desktop_rows = ""
    total_hoy = 0

    for r in rows:
        capital = r["amount"] / max(r["term_count"] or 1, 1)
        interes = r["amount"] * (r["rate"] or 0) / 100
        total = capital + interes

        if r["pagado_hoy"] == 0:
            total_hoy += total

        if r["pagado_hoy"] > 0:
            action = "<b style='color:green'>✔ Pagado</b>"
            if user["role"] in ("admin", "supervisor"):
                action += f"""
                <form method="POST"
                      action="/payment/undo/{r['loan_id']}"
                      style="display:inline"
                      onsubmit="return confirm('¿Deshacer este cobro?');">
                  <button type="submit" class="btn btn-danger btn-sm">
                    ↩ Deshacer
                  </button>
                </form>
                """
        else:
            action = f"""
            <form method="POST"
                  action="/bank/daily-list"
                  onsubmit="return confirm('¿Confirmar cobro de este cliente?');">
              <input type="hidden" name="loan_id" value="{r['loan_id']}">
              <input type="hidden" name="confirm_pay" value="yes">
              <button type="submit" class="btn btn-success btn-sm">
                📲 Marcar cobrado
              </button>
            </form>
            """


        mobile_rows += f"""
<div class="mobile-card">
  <div class="mobile-name">{r['first_name']} {r['last_name']}</div>
  <div class="mobile-phone">📞 {r['phone'] or '-'}</div>
  <div class="mobile-total">💰 {fmt_money(total)}</div>
  <div>{action}</div>
</div>
"""

        desktop_rows += f"""
<tr>
  <td>{r['route'] or ''}</td>
  <td>{r['cobrador']}</td>
  <td>{r['first_name']} {r['last_name']}</td>
  <td>{r['phone'] or ''}</td>
  <td>{fmt_money(capital)}</td>
  <td>{fmt_money(interes)}</td>
  <td><b>{fmt_money(total)}</b></td>
  <td>{action}</td>
</tr>
"""

    return mobile_css + f"""
<div class="card">
  <h3>📋 Lista de Cobro del Día</h3>
  {filter_html}
  <div class="mobile-list">{mobile_rows or "<p>No hay cobros</p>"}</div>
  <table>
    <tr>
      <th>Ruta</th><th>Cobrador</th><th>Cliente</th><th>Teléfono</th>
      <th>Capital</th><th>Interés</th><th>Total</th><th>Acciones</th>
    </tr>
    {desktop_rows}
  </table>

  <div style="text-align:right;margin-top:18px;font-size:20px;font-weight:900;color:#065f46;">
    💰 Total a cobrar hoy: {fmt_money(total_hoy)}
  </div>
</div>
"""


# ============================================================
# 🔄 DESHACER COBRO
# ============================================================
@app.route("/payment/undo/<int:loan_id>", methods=["POST"])
@login_required
def undo_payment(loan_id):
    user = current_user()

    if user["role"] not in ("admin", "supervisor"):
        flash("⛔ No autorizado.", "danger")
        return redirect(url_for("bank_daily_list"))

    conn = get_conn()
    cur = conn.cursor()

    # 🔎 BUSCAR EL ÚLTIMO PAGO ACTIVO (SIN IMPORTAR FECHA)
    cur.execute("""
        SELECT id
        FROM payments
        WHERE loan_id=%s
          AND status <> 'ANULADO'
        ORDER BY date DESC
        LIMIT 1
    """, (loan_id,))
    p = cur.fetchone()

    if not p:
        cur.close()
        conn.close()
        flash("No hay pagos para deshacer.", "warning")
        return redirect(url_for("bank_daily_list"))

    # 🔄 ANULAR PAGO
    cur.execute("""
        UPDATE payments
        SET status='ANULADO',
            note='Cobro deshecho desde lista diaria',
            edited_by=%s,
            edited_at=NOW()
        WHERE id=%s
    """, (user["id"], p["id"]))

    conn.commit()
    cur.close()
    conn.close()

    flash("↩️ Cobro deshecho correctamente.", "success")
    return redirect(url_for("bank_daily_list"))


   

# ============================================================
# 🧾 /bank/expenses  (GASTOS DE RUTA – COMPLETO Y BLINDADO)
# ============================================================
@app.route("/bank/expenses", methods=["GET", "POST"])
@login_required
def bank_expenses():
    user = current_user()
    conn = get_conn()
    cur = conn.cursor()

    # -------------------------------
    # ASEGURAR TABLA route_expenses
    # -------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS route_expenses (
            id SERIAL PRIMARY KEY,
            route VARCHAR(120) NOT NULL,
            expense_type VARCHAR(50) NOT NULL,
            amount NUMERIC(12,2) NOT NULL,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()

    # -------------------------------
    # GUARDAR GASTO (POST)
    # -------------------------------
    if request.method == "POST":

        ruta         = request.form.get("route")
        expense_type = request.form.get("expense_type")
        amount       = request.form.get("amount", type=float)
        nota         = request.form.get("note")

        if not ruta or not expense_type or not amount or amount <= 0:
            flash("⚠️ Datos incompletos o monto inválido.", "warning")
        else:
            # 1️⃣ Guardar en route_expenses (historial)
            cur.execute("""
                INSERT INTO route_expenses (route, expense_type, amount, note)
                VALUES (%s,%s,%s,%s)
            """, (ruta, expense_type, amount, nota))

            # 2️⃣ Guardar en cash_reports (ACTA)
            cur.execute("""
                INSERT INTO cash_reports (
                    user_id,
                    movement_type,
                    amount,
                    ruta,
                    note,
                    created_at
                )
                VALUES (%s, 'gasto_ruta', %s, %s, %s, NOW())
            """, (
                user["id"],
                -abs(amount),            # 🔴 SIEMPRE NEGATIVO
                ruta,                    # ⚠️ MISMO NOMBRE DE RUTA
                nota or 'Gasto de ruta'
            ))

            conn.commit()
            flash("✅ Gasto de ruta registrado correctamente.", "success")

        cur.close()
        conn.close()
        return redirect(url_for("bank_expenses"))

    # -------------------------------
    # LISTAR GASTOS (GET)  ✅ FIX CLAVE
    # -------------------------------
    cur.execute("""
        SELECT id, route, expense_type, amount, note, created_at
        FROM route_expenses
        ORDER BY created_at DESC
        LIMIT 100
    """)
    rows = cur.fetchall() or []

    rows_html = "".join(
        f"""
        <tr>
          <td>{r['route']}</td>
          <td>{r['expense_type']}</td>
          <td style="text-align:right;">💵 {fmt_money(r['amount'])}</td>
          <td>{r['note'] or ''}</td>
          <td>{r['created_at'].strftime('%Y-%m-%d %I:%M %p')}</td>
          <td style="white-space:nowrap;">
            <a class="btn btn-secondary btn-sm"
               href="/bank/expenses/edit/{r['id']}">✏️</a>
            <form method="post"
                  action="/bank/expenses/delete/{r['id']}"
                  style="display:inline"
                  onsubmit="return confirm('¿Eliminar este gasto?');">
              <button class="btn btn-danger btn-sm">🗑️</button>
            </form>
          </td>
        </tr>
        """
        for r in rows
    )

    body = f"""
    <div class="card">
      <h3>🧾 Registrar gasto de ruta</h3>

      <form method="post">
        <label>Ruta</label>
        <input name="route" placeholder="Ej: Ruta Norte" required>

        <label>Tipo de gasto</label>
        <select name="expense_type" required>
          <option value="">-- Seleccione --</option>
          <option>⛽ Gasolina</option>
          <option>🛣️ Peaje</option>
          <option>🍔 Comida</option>
          <option>🔧 Otros</option>
        </select>

        <label>Monto</label>
        <input type="number" step="0.01" name="amount" required>

        <label>Nota</label>
        <input name="note" placeholder="Opcional">

        <button class="btn btn-primary" style="margin-top:10px;">
          ➕ Guardar gasto
        </button>
      </form>
    </div>

    <div class="card">
      <h3>📋 Gastos registrados</h3>
      <table>
        <tr>
          <th>Ruta</th>
          <th>Tipo</th>
          <th>Monto</th>
          <th>Nota</th>
          <th>Fecha</th>
          <th></th>
        </tr>
        {rows_html or "<tr><td colspan='6'>Sin gastos</td></tr>"}
      </table>
    </div>
    """

    cur.close()
    conn.close()

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )
    
# ============================================================
# 🗑️ BORRAR GASTO DE RUTA (FIX POSTGRES)
# ============================================================
@app.route("/bank/expenses/delete/<int:expense_id>", methods=["POST"])
@login_required
def delete_route_expense(expense_id):

    conn = get_conn()
    cur = conn.cursor()

    # 1️⃣ Obtener el gasto
    cur.execute("""
        SELECT route, amount, note
        FROM route_expenses
        WHERE id = %s
    """, (expense_id,))
    expense = cur.fetchone()

    if not expense:
        flash("Gasto no encontrado.", "warning")
        cur.close()
        conn.close()
        return redirect(url_for("bank_expenses"))

    ruta  = expense["route"]
    monto = float(expense["amount"])
    nota  = expense["note"] or 'Gasto de ruta'

    # 2️⃣ Borrar de route_expenses
    cur.execute("""
        DELETE FROM route_expenses
        WHERE id = %s
    """, (expense_id,))

    # 3️⃣ Borrar SOLO UN gasto correspondiente en cash_reports (POSTGRES SAFE)
    cur.execute("""
        DELETE FROM cash_reports
        WHERE id = (
            SELECT id
            FROM cash_reports
            WHERE
              movement_type = 'gasto_ruta'
              AND TRIM(ruta) = TRIM(%s)
              AND ABS(amount) = ABS(%s)
              AND COALESCE(note,'') = COALESCE(%s,'')
            ORDER BY created_at DESC
            LIMIT 1
        )
    """, (ruta, monto, nota))

    conn.commit()
    cur.close()
    conn.close()

    flash("🗑️ Gasto eliminado y acta actualizada correctamente.", "success")
    return redirect(url_for("bank_expenses"))




# ============================================================
# ✏️ EDITAR GASTO
# ============================================================
@app.route("/bank/expenses/edit/<int:expense_id>", methods=["GET", "POST"])
@login_required
def edit_expense(expense_id):
    user = current_user()
    if user["role"] not in ("admin", "supervisor"):
        flash("⛔ No autorizado.", "danger")
        return redirect(url_for("bank_expenses"))

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM route_expenses WHERE id=%s", (expense_id,))
    exp = cur.fetchone()

    if not exp:
        flash("Gasto no encontrado.", "danger")
        return redirect(url_for("bank_expenses"))

    if request.method == "POST":
        expense_type = request.form.get("expense_type")
        amount = request.form.get("amount", type=float)
        note = request.form.get("note", "")

        cur.execute("""
            UPDATE route_expenses
            SET expense_type=%s, amount=%s, note=%s
            WHERE id=%s
        """, (expense_type, amount, note, expense_id))
        conn.commit()
        flash("✏️ Gasto actualizado.", "success")
        return redirect(url_for("bank_expenses"))

    cur.close()
    conn.close()

    body = f"""
    <div class="card">
      <h3>✏️ Editar gasto</h3>
      <form method="post">
        <label>Tipo</label>
        <input name="expense_type" value="{exp['expense_type']}">

        <label>Monto</label>
        <input type="number" step="0.01" name="amount" value="{exp['amount']}">

        <label>Nota</label>
        <input name="note" value="{exp['note'] or ''}">

        <button class="btn btn-primary">Guardar cambios</button>
      </form>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )

@app.route("/route/expenses/new", methods=["POST"])
@login_required
def add_route_expense():
    conn = get_conn()
    cur = conn.cursor()

    ruta   = request.form.get("route")
    monto  = request.form.get("amount", type=float)
    tipo   = request.form.get("expense_type")
    nota   = request.form.get("note", "")
    user_id = current_user()["id"]

    if not monto or monto <= 0:
        cur.close()
        conn.close()
        flash("⚠️ Monto inválido.", "danger")
        return redirect(url_for("route_expenses"))

    # ===============================
    # 🔒 VALIDAR FONDO DISPONIBLE (CAJA GLOBAL)
    # ===============================
    cur.execute("""
        SELECT
        (
          SELECT COALESCE(SUM(ABS(amount)),0)
          FROM cash_reports
          WHERE movement_type = 'descuento_inicial'
        )
        -
        (
          SELECT COALESCE(SUM(ABS(amount)),0)
          FROM cash_reports
          WHERE movement_type = 'gasto_ruta'
        ) >= %s
    """, (monto,))

    puede_gastar = cur.fetchone()[0]

    if not puede_gastar:
        cur.close()
        conn.close()
        flash("⚠️ No hay fondo disponible en caja.", "danger")
        return redirect(url_for("route_expenses"))

    # ===============================
    # 💾 REGISTRAR GASTO (HISTORIAL)
    # ===============================
    cur.execute("""
        INSERT INTO route_expenses
        (route, expense_type, amount, note, user_id, created_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
    """, (ruta or 'GLOBAL', tipo, monto, nota, user_id))

    # ===============================
    # 💸 IMPACTO EN CAJA (ACTA)
    # ===============================
    cur.execute("""
        INSERT INTO cash_reports (
            user_id,
            movement_type,
            amount,
            ruta,
            note,
            created_at
        )
        VALUES (%s, 'gasto_ruta', %s, %s, %s, NOW())
    """, (
        user_id,
        -abs(monto),          # 🔴 siempre negativo
        ruta or 'GLOBAL',     # solo informativo
        nota or f"Gasto - {tipo}"
    ))

    conn.commit()
    cur.close()
    conn.close()

    flash("✅ Gasto registrado correctamente.", "success")
    return redirect(url_for("route_expenses"))


    
# ============================================================
#  ELIMINAR DESCUENTO INICIAL
# ============================================================
@app.route("/bank/discount/delete/<int:discount_id>", methods=["POST"])
@login_required
def delete_discount(discount_id):

    user = current_user()

    if user["role"] not in ("admin", "supervisor"):
        flash("No tienes permiso para eliminar descuentos.", "danger")
        return redirect(url_for("bank_acta"))

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT amount
        FROM cash_reports
        WHERE id=%s
          AND movement_type='descuento_inicial'
    """, (discount_id,))
    row = cur.fetchone()

    if not row:
        flash("Descuento no encontrado.", "danger")
        cur.close()
        conn.close()
        return redirect(url_for("bank_acta"))

    amount = row["amount"]

    cur.execute("""
        DELETE FROM cash_reports
        WHERE id=%s
          AND movement_type='descuento_inicial'
    """, (discount_id,))

    log_action(
        user["id"],
        "delete_discount",
        f"Eliminó descuento inicial de {fmt_money(amount)}"
    )

    conn.commit()
    cur.close()
    conn.close()

    flash("✅ Descuento eliminado correctamente.", "success")
    return redirect(url_for("bank_acta"))


# ============================================================
# 🗑️ ELIMINAR GASTO
# ============================================================
@app.route("/bank/expenses/delete/<int:expense_id>", methods=["POST"])
@login_required
def delete_expense(expense_id):
    user = current_user()
    if user["role"] not in ("admin", "supervisor"):
        flash("⛔ No autorizado.", "danger")
        return redirect(url_for("bank_expenses"))

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM route_expenses WHERE id=%s", (expense_id,))
    conn.commit()
    cur.close()
    conn.close()

    flash("🗑️ Gasto eliminado.", "success")
    return redirect(url_for("bank_expenses"))
    
# ============================================================
# 📊 /bank/routes/history — HISTORIAL DIARIO POR RUTA + COBRADOR
# ============================================================
@app.route("/bank/routes/history")
@login_required
def bank_routes_history():

    user = current_user()
    conn = get_conn()
    cur = conn.cursor()

    # ===============================
    # 🔎 FILTRO POR COBRADOR
    # ===============================
    filter_user = request.args.get("filter_user", type=int)
    user_filter_sql = ""
    params = []

    if filter_user:
        user_filter_sql = "AND u.id = %s"
        params.append(filter_user)

    # ===============================
    # 👤 LISTA DE COBRADORES
    # ===============================
    cur.execute("""
        SELECT id, username
        FROM users
        WHERE role = 'cobrador'
        ORDER BY username
    """)
    cobradores = cur.fetchall() or []

    # ===============================
    # 📊 CONSULTA PRINCIPAL (BLINDADA)
    # ===============================
    cur.execute(f"""
        SELECT
            TRIM(UPPER(COALESCE(c.route,'SIN RUTA'))) AS ruta,
            u.username                               AS cobrador,

            -- 💰 Capital REAL en calle (sin interés)
            COALESCE(SUM(l.remaining),0)             AS capital_en_calle,

            -- 🔴 Cobrado HOY
            COALESCE(SUM(
                CASE
                  WHEN p.id IS NOT NULL
                   AND DATE(p.date) = CURRENT_DATE
                  THEN p.amount
                  ELSE 0
                END
            ),0) AS recogido_hoy,

            -- 🟢 Prestado HOY
            COALESCE(SUM(
                CASE
                  WHEN DATE(l.start_date) = CURRENT_DATE
                  THEN l.amount
                  ELSE 0
                END
            ),0) AS prestado_hoy

        FROM loans l
        JOIN clients c ON c.id = l.client_id
        JOIN users u   ON u.id = l.created_by
        LEFT JOIN payments p ON p.loan_id = l.id

        WHERE UPPER(l.status) = 'ACTIVO'
        {user_filter_sql}

        GROUP BY ruta, u.username
        ORDER BY ruta, u.username
    """, params)

    rows = cur.fetchall() or []

    cur.close()
    conn.close()

    # ===============================
    # 🎛️ HTML — FILTRO COBRADOR
    # ===============================
    filter_opts = "<option value=''>-- TODOS --</option>" + "".join(
        f"<option value='{c['id']}' {'selected' if filter_user == c['id'] else ''}>{c['username']}</option>"
        for c in cobradores
    )

    filter_html = f"""
    <div class="card">
      <h3>👤 Filtrar por cobrador</h3>
      <select onchange="location.href='/bank/routes/history' + (this.value ? '?filter_user=' + this.value : '')">
        {filter_opts}
      </select>
    </div>
    """

    # ===============================
    # 🧾 CARDS — CÁLCULO FINANCIERO REAL
    # ===============================
    cards_html = ""

    for r in rows:
        capital_en_calle = float(r["capital_en_calle"] or 0)
        recogido_hoy     = float(r["recogido_hoy"] or 0)
        prestado_hoy     = float(r["prestado_hoy"] or 0)

        # 💰 Balance real del día
        balance_actual = capital_en_calle - recogido_hoy + prestado_hoy

        cards_html += f"""
        <div class="card" style="margin-bottom:15px;">
          <h3>📍 {r['ruta']}</h3>
          <p><b>👤 Cobrador:</b> {r['cobrador']}</p>

          <table>
            <tr>
              <th align="left">Capital en calle</th>
              <td>{fmt_money(capital_en_calle)}</td>
            </tr>
            <tr>
              <th align="left">Recogido hoy</th>
              <td style="color:red">-{fmt_money(recogido_hoy)}</td>
            </tr>
            <tr>
              <th align="left">Prestado hoy</th>
              <td style="color:green">+{fmt_money(prestado_hoy)}</td>
            </tr>
            <tr><th colspan="2"><hr></th></tr>
            <tr>
              <th align="left">💰 Balance actual</th>
              <td><b>{fmt_money(balance_actual)}</b></td>
            </tr>
          </table>
        </div>
        """

    # ===============================
    # 🎨 HTML FINAL
    # ===============================
    body = f"""
    <div class="card" style="text-align:center;">
      <h2>📊 Historial diario por ruta</h2>
      <div style="color:#6b7280;font-size:14px;">
        Capital real • Recogido hoy • Prestado hoy • Balance
      </div>
    </div>

    {filter_html}

    {cards_html or "<div class='card'>No hay datos</div>"}
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )




# ============================================================
#  💰 ENTREGA / DEVOLUCIÓN DE EFECTIVO + BALANCE + TOTAL PRESTADO
#  + CAPITAL / INTERÉS + FILTRO POR COBRADOR + HISTORIAL
# ============================================================
@app.route("/bank/delivery", methods=["GET", "POST"])
@login_required
def bank_delivery():
    conn = get_conn()
    cur = conn.cursor()

    # ===============================
    # FILTRO POR COBRADOR
    # ===============================
    filter_user = request.args.get("filter_user", type=int)

    # ===============================
    # OBTENER COBRADORES
    # ===============================
    cur.execute("""
        SELECT id, username
        FROM users
        WHERE role='cobrador'
        ORDER BY username
    """)
    cobradores = cur.fetchall()

    # ===============================
    # REGISTRAR MOVIMIENTO
    # ===============================
    if request.method == "POST":
        user_id = request.form.get("user_id", type=int)
        movement_type = request.form.get("movement_type")
        amount = request.form.get("amount", type=float)
        note = request.form.get("note", "").strip()

        if not user_id or movement_type not in ("entrega", "devolucion") or not amount or amount <= 0:
            flash("Datos inválidos.", "danger")
        else:
            cur.execute("""
                INSERT INTO cash_reports (user_id, movement_type, amount, note, created_at)
                VALUES (%s,%s,%s,%s,NOW())
            """, (user_id, movement_type, amount, note))
            conn.commit()
            flash("Movimiento registrado correctamente.", "success")

        return redirect(
            url_for("bank_delivery", filter_user=filter_user)
            if filter_user else url_for("bank_delivery")
        )

    # ===============================
    # HISTORIAL
    # ===============================
    if filter_user:
        cur.execute("""
            SELECT cr.id, cr.created_at, cr.movement_type,
                   cr.amount, cr.note, u.username
            FROM cash_reports cr
            LEFT JOIN users u ON u.id=cr.user_id
            WHERE cr.user_id=%s
            ORDER BY cr.created_at DESC
        """, (filter_user,))
    else:
        cur.execute("""
            SELECT cr.id, cr.created_at, cr.movement_type,
                   cr.amount, cr.note, u.username
            FROM cash_reports cr
            LEFT JOIN users u ON u.id=cr.user_id
            ORDER BY cr.created_at DESC
        """)
    rows = cur.fetchall()

    # ===============================
    # BALANCE DEL DÍA
    # ===============================
    if filter_user:
        cur.execute("""
            SELECT
              COALESCE(SUM(CASE WHEN movement_type='entrega' THEN amount END),0) entregado,
              COALESCE(SUM(CASE WHEN movement_type='devolucion' THEN amount END),0) devuelto
            FROM cash_reports
            WHERE DATE(created_at)=CURRENT_DATE AND user_id=%s
        """, (filter_user,))
    else:
        cur.execute("""
            SELECT
              COALESCE(SUM(CASE WHEN movement_type='entrega' THEN amount END),0) entregado,
              COALESCE(SUM(CASE WHEN movement_type='devolucion' THEN amount END),0) devuelto
            FROM cash_reports
            WHERE DATE(created_at)=CURRENT_DATE
        """)
    b = cur.fetchone()
    total_entregado = float(b["entregado"])
    total_devuelto = float(b["devuelto"])
    balance_dia = total_devuelto - total_entregado

    # ===============================
    # TOTAL PRESTADO
    # ===============================
    if filter_user:
        cur.execute(
            "SELECT COALESCE(SUM(amount),0) AS t FROM loans WHERE created_by=%s",
            (filter_user,)
        )
    else:
        cur.execute("SELECT COALESCE(SUM(amount),0) AS t FROM loans")

    total_prestado = float(cur.fetchone()["t"])

    # ===============================
    # CAPITAL E INTERÉS
    # ===============================
    if filter_user:
        cur.execute("""
            SELECT
              COALESCE(SUM(amount),0) AS capital_original,
              COALESCE(SUM(remaining),0) AS capital_pendiente
            FROM loans
            WHERE created_by=%s
        """, (filter_user,))
    else:
        cur.execute("""
            SELECT
              COALESCE(SUM(amount),0) AS capital_original,
              COALESCE(SUM(remaining),0) AS capital_pendiente
            FROM loans
        """)

    cap = cur.fetchone()
    capital_original = float(cap["capital_original"])
    capital_pendiente = float(cap["capital_pendiente"])

    if filter_user:
        cur.execute("""
            SELECT COALESCE(SUM(amount * rate / 100 * term_count),0) AS interes_total
            FROM loans
            WHERE created_by=%s
        """, (filter_user,))
    else:
        cur.execute("""
            SELECT COALESCE(SUM(amount * rate / 100 * term_count),0) AS interes_total
            FROM loans
        """)

    interes_total = float(cur.fetchone()["interes_total"])

    if filter_user:
        cur.execute("""
            SELECT COALESCE(SUM(p.amount),0) AS total_pagado
            FROM payments p
            JOIN loans l ON l.id=p.loan_id
            WHERE l.created_by=%s
        """, (filter_user,))
    else:
        cur.execute("""
            SELECT COALESCE(SUM(amount),0) AS total_pagado
            FROM payments
        """)

    total_pagado = float(cur.fetchone()["total_pagado"])

    capital_cobrado = capital_original - capital_pendiente
    interes_cobrado = max(total_pagado - capital_cobrado, 0)
    interes_pendiente = max(interes_total - interes_cobrado, 0)
    total_pendiente = capital_pendiente + interes_pendiente

    filter_label = "TODOS"
    if filter_user:
        for c in cobradores:
            if c["id"] == filter_user:
                filter_label = c["username"]
                break

    filter_opts = "<option value=''>-- TODOS --</option>" + "".join(
        f"<option value='{c['id']}' {'selected' if filter_user==c['id'] else ''}>{c['username']}</option>"
        for c in cobradores
    )


    history = "".join(
        f"""
        <tr>
          <td>{r['created_at'].strftime('%Y-%m-%d')}</td>
          <td>{r['created_at'].strftime('%I:%M %p')}</td>
          <td>{"↩️ Devolución" if r['movement_type']=="devolucion" else "💼 Entrega"}</td>
          <td>{r.get("username") or "ADMIN"}</td>
          <td>{fmt_money(r["amount"])}</td>
          <td>{r.get("note") or ""}</td>
          <td style="white-space:nowrap;">
            <a class="btn btn-secondary btn-sm"
               href="/bank/delivery/edit/{r['id']}">✏️</a>

            {(
                f'''
                <form method="post"
                      action="/bank/delivery/delete/{r['id']}"
                      style="display:inline"
                      onsubmit="return confirm('¿Eliminar este movimiento?');">
                  <button class="btn btn-danger btn-sm">🗑️</button>
                </form>
                '''
                if current_user().get("role") == "admin"
                else ""
            )}
          </td>
        </tr>
        """
        for r in rows
    )


    body = f"""
    <div class="card">
      <h3>💰 Entrega / Devolución de Efectivo</h3>
      <form method="post" class="row g-2">
        <select name="user_id" required>
          <option value="">-- Cobrador --</option>
          {''.join(f"<option value='{c['id']}'>{c['username']}</option>" for c in cobradores)}
        </select>

        <select name="movement_type" required>
          <option value="entrega">💼 Entrega</option>
          <option value="devolucion">↩️ Devolución</option>
        </select>

        <input type="number" step="0.01" name="amount" placeholder="Monto" required>
        <input type="text" name="note" placeholder="Nota">

        <button class="btn btn-success">Guardar</button>
      </form>
    </div>

    <div class="card">
      <h3>📊 Balance del Día – {filter_label}</h3>
      <select onchange="location.href='/bank/delivery'+(this.value?'?filter_user='+this.value:'')">
        {filter_opts}
      </select>
      <table>
        <tr><th>Entregado</th><td>{fmt_money(total_entregado)}</td></tr>
        <tr><th>Devuelto</th><td>{fmt_money(total_devuelto)}</td></tr>
        <tr><th>Balance</th><td><b>{fmt_money(balance_dia)}</b></td></tr>
      </table>
    </div>

    <div class="card">
      <h3>📋 Historial de Movimientos</h3>
      <table>
        <tr>
          <th>Fecha</th><th>Hora</th><th>Tipo</th>
          <th>Cobrador</th><th>Monto</th><th>Nota</th><th></th>
        </tr>
        {history or "<tr><td colspan='7'>Sin movimientos</td></tr>"}
      </table>
    </div>
    """

    cur.close()
    conn.close()

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=current_user(),
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )

# ============================================================
# ✏️ EDITAR ENTREGA (REAL)
# ============================================================
@app.route("/bank/delivery/edit/<int:delivery_id>", methods=["GET", "POST"])
@login_required
def bank_delivery_edit(delivery_id):
    conn = get_conn()
    cur = conn.cursor()

    if request.method == "POST":
        amount = request.form.get("amount", type=float)
        note = request.form.get("note", "").strip()

        if not amount or amount <= 0:
            flash("Monto inválido.", "danger")
        else:
            cur.execute("""
                UPDATE cash_reports
                SET amount=%s, note=%s
                WHERE id=%s
            """, (amount, note, delivery_id))
            conn.commit()
            flash("Entrega actualizada.", "success")

        cur.close()
        conn.close()
        return redirect(url_for("bank_delivery"))

    cur.execute("SELECT * FROM cash_reports WHERE id=%s", (delivery_id,))
    delivery = cur.fetchone()
    cur.close()
    conn.close()

    if not delivery:
        flash("Entrega no encontrada.", "danger")
        return redirect(url_for("bank_delivery"))

    body = f"""
    <div class="card">
      <h3>✏️ Editar entrega #{delivery_id}</h3>

      <form method="post">
        <label>Monto</label>
        <input type="number" step="0.01" name="amount"
               value="{delivery['amount']}" required>

        <label>Nota</label>
        <input type="text" name="note"
               value="{delivery.get('note') or ''}">

        <button class="btn btn-success">Guardar cambios</button>
        <a class="btn btn-secondary" href="/bank/delivery">Cancelar</a>
      </form>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=current_user(),
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )

# ============================================================
# 🗑️ ELIMINAR ENTREGA (SOLO ADMIN)
# ============================================================
@app.route("/bank/delivery/delete/<int:delivery_id>", methods=["POST"])
@login_required
def bank_delivery_delete(delivery_id):

    if current_user().get("role") != "admin":
        flash("⛔ No tienes permiso para eliminar movimientos.", "danger")
        return redirect(url_for("bank_delivery"))

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("DELETE FROM cash_reports WHERE id=%s", (delivery_id,))
    conn.commit()

    cur.close()
    conn.close()

    flash("🗑️ Entrega eliminada correctamente.", "success")
    return redirect(url_for("bank_delivery"))


# ============================================================
# 🏦 ACTA GLOBAL REAL (USA TABLAS REALES DEL SISTEMA)
# ============================================================
@app.route("/bank/acta")
@login_required
def bank_acta():

    from psycopg2.extras import RealDictCursor

    user = current_user()
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # ============================================================
    # 💸 DESCUENTO TOTAL REAL (initial_discounts)
    # ============================================================
    cur.execute("""
        SELECT COALESCE(SUM(amount),0) AS total
        FROM initial_discounts
    """)
    descuento_total = float(cur.fetchone()["total"])

    # ============================================================
    # 📤 GASTOS REALES (route_expenses)
    # ============================================================
    cur.execute("""
        SELECT COALESCE(SUM(amount),0) AS total
        FROM route_expenses
    """)
    gastos_total = float(cur.fetchone()["total"])

    disponible = descuento_total - gastos_total

    # ============================================================
    # 📄 LISTA DESCUENTOS REALES
    # ============================================================
    cur.execute("""
        SELECT
            d.id,
            d.created_at,
            u.username AS collector_name,
            ru.username AS route_name,
            d.amount
        FROM initial_discounts d
        LEFT JOIN users u ON u.id = d.collector_id
        LEFT JOIN routes r ON r.id = d.route_id
        LEFT JOIN users ru ON ru.id = r.user_id
        ORDER BY d.created_at DESC
        LIMIT 200
    """)

    rows = cur.fetchall() or []

    rows_html = "".join(
        f"""
        <tr>
          <td>{r['created_at'].strftime('%d/%m/%Y %I:%M %p') if r['created_at'] else ''}</td>
          <td>{r.get('collector_name') or 'Admin'}</td>
          <td>{r.get('route_name') or ''}</td>
          <td style="color:#dc2626;font-weight:900;">
            -{fmt_money(abs(r['amount']))}
          </td>
          <td>
            <form method="post"
                  action="{url_for('delete_discount', discount_id=r['id'])}"
                  onsubmit="return confirm('¿Eliminar este descuento?');">
              <button class="btn btn-danger btn-sm">🗑️</button>
            </form>
          </td>
        </tr>
        """
        for r in rows
    ) or "<tr><td colspan='5'>Sin movimientos</td></tr>"

    cur.close()
    conn.close()

    # ============================================================
    # UI
    # ============================================================
    body = f"""
    <div class="card">
      <h2>💸 Acta global</h2>
      <a class="btn btn-secondary" href="/bank">⬅️ Volver</a>

      <div class="card" style="margin-bottom:18px;">
        <h3>📊 Caja global</h3>

        <div style="display:flex;justify-content:space-between;">
          <span>Descuento total:</span>
          <strong>{fmt_money(descuento_total)}</strong>
        </div>

        <div style="display:flex;justify-content:space-between;">
          <span>Gastos realizados:</span>
          <strong>{fmt_money(gastos_total)}</strong>
        </div>

        <hr>

        <div style="display:flex;justify-content:space-between;font-size:18px;">
          <strong>Disponible:</strong>
          <strong style="color:{'green' if disponible >= 0 else 'red'};">
            {fmt_money(disponible)}
            {'🟢' if disponible >= 0 else '🔴'}
          </strong>
        </div>
      </div>

      <div class="card" style="margin-top:20px;">
        <h3>📋 Descuentos registrados</h3>
        <table>
          <tr>
            <th>Fecha</th>
            <th>Cobrador</th>
            <th>Ruta</th>
            <th>Monto</th>
            <th></th>
          </tr>
          {rows_html}
        </table>
      </div>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )
    
# ============================================================
# 🏦 BANCO → CAPITAL POR RUTA (FILTRO POR PRESTAMISTA)
# ============================================================
@app.route("/bank/routes")
@login_required
def bank_routes_list():
    conn = get_conn()
    cur = conn.cursor()

    # ===============================
    # 🔎 FILTRO POR PRESTAMISTA
    # ===============================
    filter_user = request.args.get("prestamista", type=int)
    user_filter_sql = ""
    params = []

    if filter_user:
        user_filter_sql = "AND u.id = %s"
        params.append(filter_user)

    # ===============================
    # 👤 LISTA DE PRESTAMISTAS
    # ===============================
    cur.execute("""
        SELECT id, username
        FROM users
        ORDER BY username
    """)
    prestamistas = cur.fetchall() or []

    # ===============================
    # 📊 TOTAL EN LA CALLE POR RUTA
    # ===============================
    cur.execute(f"""
        SELECT
            u.username AS prestamista,
            TRIM(UPPER(COALESCE(c.route, 'SIN RUTA'))) AS ruta,
            COALESCE(SUM(l.total_to_pay),0)
            - COALESCE(SUM(p.total_pagado),0) AS total_en_calle
        FROM loans l
        JOIN users u   ON u.id = l.created_by
        JOIN clients c ON c.id = l.client_id
        LEFT JOIN (
            SELECT loan_id, SUM(amount) AS total_pagado
            FROM payments
            GROUP BY loan_id
        ) p ON p.loan_id = l.id
        WHERE UPPER(l.status) = 'ACTIVO'
        {user_filter_sql}
        GROUP BY u.username, TRIM(UPPER(COALESCE(c.route, 'SIN RUTA')))
        ORDER BY u.username, ruta
    """, params)

    rows = cur.fetchall() or []
    cur.close()
    conn.close()

    # ===============================
    # 🔽 HTML — SELECT PRESTAMISTA
    # ===============================
    options_html = "<option value=''>-- TODOS --</option>" + "".join(
        f"<option value='{p['id']}' {'selected' if filter_user == p['id'] else ''}>{p['username']}</option>"
        for p in prestamistas
    )

    filter_html = f"""
    <div class="card">
      <label><b>👤 Filtrar por prestamista</b></label>
      <select onchange="location.href='/bank/routes' + (this.value ? '?prestamista=' + this.value : '')">
        {options_html}
      </select>
    </div>
    """

    # ===============================
    # 🎴 CARDS
    # ===============================
    cards_html = ""

    for r in rows:
        prestamista = (r.get("prestamista") or "SIN PRESTAMISTA").upper()
        ruta = (r.get("ruta") or "SIN RUTA").upper()
        total = float(r.get("total_en_calle") or 0)

        cards_html += f"""
        <div class="card" style="text-align:center; margin-bottom:18px;">
          <div style="font-size:14px; opacity:0.6;">
            👤 {prestamista}
          </div>

          <div style="font-size:18px; opacity:0.7; margin-top:4px;">
            📍 {ruta}
          </div>

          <div style="font-size:34px; font-weight:900; margin-top:8px;">
            {fmt_money(total)}
          </div>
        </div>
        """

    # ===============================
    # 🎨 HTML FINAL
    # ===============================
    body = f"""
    <div class="card" style="text-align:center;">
      <h2>🧭 Capital por ruta</h2>
      <a class="btn btn-secondary" href="/bank">⬅️ Volver</a>
    </div>

    {filter_html}

    {cards_html if cards_html else "<div class='card'>No hay datos para este prestamista</div>"}
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=current_user(),
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )


# ============================================================
# ⚠️ ATRASOS (CUOTAS ATRASADAS ENUMERADAS + PAGAR)
# ============================================================
from datetime import date, datetime, timedelta

@app.route("/bank/late")
@login_required
def bank_late():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            l.id AS loan_id,
            c.first_name,
            c.last_name,
            c.phone,
            l.start_date,
            l.frequency,
            l.term_count,
            l.amount,
            l.remaining,
            COUNT(p.id) AS pagos_realizados
        FROM loans l
        JOIN clients c ON c.id = l.client_id
        LEFT JOIN payments p ON p.loan_id = l.id
        WHERE l.status = 'ACTIVO'
        GROUP BY
            l.id, c.first_name, c.last_name, c.phone,
            l.start_date, l.frequency, l.term_count,
            l.amount, l.remaining
        ORDER BY l.start_date ASC
    """)

    loans = cur.fetchall() or []
    today = date.today()
    rows = ""

    for l in loans:
        start_date = l.get("start_date")

        if not start_date:
            continue

        # ====================================================
        # CONVERTIR start_date A date (ULTRA SEGURO)
        # ====================================================
        if isinstance(start_date, str):
            try:
                start_date = datetime.fromisoformat(start_date).date()
            except:
                continue
        elif hasattr(start_date, "date"):
            start_date = start_date.date()

        pagos = int(l.get("pagos_realizados") or 0)
        term_count = int(l.get("term_count") or 1)
        frequency = (l.get("frequency") or "").strip().lower()

        # ====================================================
        # DEFINIR INTERVALO SEGÚN FRECUENCIA
        # ====================================================
        if frequency == "diario":
            delta = timedelta(days=1)
        elif frequency == "semanal":
            delta = timedelta(weeks=1)
        elif frequency == "quincenal":
            delta = timedelta(days=14)
        elif frequency == "mensual":
            delta = timedelta(days=30)
        else:
            continue

        # ====================================================
        # PRIMERA CUOTA PENDIENTE
        # ====================================================
        next_due = start_date + (delta * (pagos + 1))

        # ====================================================
        # SI NO ESTÁ ATRASADO → saltar
        # ====================================================
        if not (today > next_due and pagos < term_count):
            continue

        # ====================================================
        # CALCULAR CUOTAS ATRASADAS (SEGURO)
        # ====================================================
        days_late = (today - next_due).days

        # evitar división por cero
        interval_days = max(delta.days, 1)

        cuotas_atrasadas = (days_late // interval_days) + 1
        cuotas_atrasadas = min(cuotas_atrasadas, term_count - pagos)

        # ====================================================
        # CREAR FILA POR CADA CUOTA ATRASADA
        # ====================================================
        for i in range(cuotas_atrasadas):
            cuota_numero = pagos + i + 1
            due_date = next_due + (delta * i)

            rows += f"""
            <tr>
              <td>{l['first_name']} {l['last_name']}</td>
              <td>Cuota atrasada #{cuota_numero}</td>
              <td>{l.get('phone') or '-'}</td>
              <td style="color:red;font-weight:bold">
                {(today - due_date).days} días
              </td>
              <td>{fmt_money(l.get('remaining', 0))}</td>
              <td>{due_date.strftime("%d/%m/%Y")}</td>
              <td>
                <a href="/payment/new/{l['loan_id']}?installment={cuota_numero}&late=1"
                style="background:#e53935;color:white;padding:6px 12px;border-radius:6px;text-decoration:none;">
                💸 Pagar
                </a>
              </td>
            </tr>
            """

    if not rows:
        rows = "<tr><td colspan='7'>Sin atrasos</td></tr>"

    body = f"""
    <div class="card">
      <h2>⚠️ Cuotas Atrasadas</h2>

      <table>
        <thead>
          <tr>
            <th>Cliente</th>
            <th>Cuota</th>
            <th>Teléfono</th>
            <th>Días atraso</th>
            <th>Saldo</th>
            <th>Fecha vencida</th>
            <th>Acción</th>
          </tr>
        </thead>
        <tbody>
          {rows}
        </tbody>
      </table>
    </div>
    """

    cur.close()
    conn.close()

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=current_user(),
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )

# ============================================================
# 🏆 RANKING CLIENTES MOROSOS
# ============================================================
from datetime import date, datetime, timedelta

@app.route("/bank/ranking")
@login_required
def bank_ranking():

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            l.id AS loan_id,
            c.first_name,
            c.last_name,
            c.phone,
            l.start_date,
            l.frequency,
            l.term_count,
            l.remaining,
            COUNT(p.id) AS pagos_realizados
        FROM loans l
        JOIN clients c ON c.id = l.client_id
        LEFT JOIN payments p ON p.loan_id = l.id
        WHERE l.status = 'ACTIVO'
        GROUP BY
            l.id, c.first_name, c.last_name, c.phone,
            l.start_date, l.frequency, l.term_count, l.remaining
    """)

    loans = cur.fetchall() or []
    today = date.today()
    ranking = []

    for l in loans:
        start_date = l.get("start_date")
        if not start_date:
            continue

        # convertir fecha seguro
        if isinstance(start_date, str):
            try:
                start_date = datetime.fromisoformat(start_date).date()
            except:
                continue
        elif hasattr(start_date, "date"):
            start_date = start_date.date()

        pagos = int(l.get("pagos_realizados") or 0)
        term_count = int(l.get("term_count") or 1)
        frequency = (l.get("frequency") or "").strip().lower()

        # intervalo
        if frequency == "diario":
            delta = timedelta(days=1)
        elif frequency == "semanal":
            delta = timedelta(weeks=1)
        elif frequency == "quincenal":
            delta = timedelta(days=14)
        elif frequency == "mensual":
            delta = timedelta(days=30)
        else:
            continue

        # próxima cuota correcta (IMPORTANTE +1)
        next_due = start_date + (delta * (pagos + 1))

        if not (today > next_due and pagos < term_count):
            continue

        days_late = (today - next_due).days
        interval_days = max(delta.days, 1)

        cuotas_atrasadas = (days_late // interval_days) + 1
        cuotas_atrasadas = min(cuotas_atrasadas, term_count - pagos)

        ranking.append({
            "loan_id": l["loan_id"],
            "name": f"{l['first_name']} {l['last_name']}",
            "phone": l.get("phone"),
            "days_late": days_late,
            "cuotas": cuotas_atrasadas,
            "saldo": l.get("remaining", 0)
        })

    # ordenar peor cliente primero
    ranking.sort(key=lambda x: (x["cuotas"], x["days_late"]), reverse=True)

    rows = ""

    for r in ranking:
        rows += f"""
        <tr>
            <td>{r['name']}</td>
            <td>{r['phone'] or '-'}</td>
            <td style="color:red;font-weight:bold">{r['cuotas']}</td>
            <td>{r['days_late']} días</td>
            <td>{fmt_money(r['saldo'])}</td>
            <td>
                <a href="/loan/{r['loan_id']}"
                style="background:#1976d2;color:white;padding:6px 12px;border-radius:6px;text-decoration:none;">
                Ver préstamo
                </a>
            </td>
        </tr>
        """

    if not rows:
        rows = "<tr><td colspan='6'>No hay clientes morosos</td></tr>"

    body = f"""
    <div class="card">
        <h2>🏆 Ranking Clientes Morosos</h2>

        <table>
            <thead>
                <tr>
                    <th>Cliente</th>
                    <th>Teléfono</th>
                    <th>Cuotas atrasadas</th>
                    <th>Días atraso</th>
                    <th>Saldo</th>
                    <th>Acción</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
    """

    cur.close()
    conn.close()

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=current_user(),
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )

# ============================================================
# 💳 HISTORIAL DE CRÉDITO CLIENTES
# ============================================================
@app.route("/bank/credit-history")
@login_required
def credit_history():

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            l.id AS loan_id,
            c.first_name,
            c.last_name,
            c.phone,
            l.start_date,
            l.frequency,
            l.term_count,
            l.remaining,
            COUNT(p.id) AS pagos_realizados
        FROM loans l
        JOIN clients c ON c.id = l.client_id
        LEFT JOIN payments p ON p.loan_id = l.id
        WHERE l.status = 'ACTIVO'
        GROUP BY
            l.id, c.first_name, c.last_name, c.phone,
            l.start_date, l.frequency, l.term_count, l.remaining
    """)

    loans = cur.fetchall() or []
    today = date.today()
    rows = ""

    for l in loans:
        start_date = l.get("start_date")
        if not start_date:
            continue

        if isinstance(start_date, str):
            start_date = datetime.fromisoformat(start_date).date()
        elif hasattr(start_date, "date"):
            start_date = start_date.date()

        pagos = int(l.get("pagos_realizados") or 0)
        term_count = int(l.get("term_count") or 1)
        frequency = (l.get("frequency") or "").strip().lower()

        # intervalo
        if frequency == "diario":
            delta = timedelta(days=1)
        elif frequency == "semanal":
            delta = timedelta(weeks=1)
        elif frequency == "quincenal":
            delta = timedelta(days=14)
        elif frequency == "mensual":
            delta = timedelta(days=30)
        else:
            continue

        next_due = start_date + (delta * (pagos + 1))

        # ===== CLASIFICACIÓN =====
        if pagos >= term_count:
            nivel = "🟢 EXCELENTE"
            color = "green"
        elif today <= next_due:
            nivel = "🟢 BUENO"
            color = "green"
        else:
            days_late = (today - next_due).days

            if days_late <= 7:
                nivel = "🟡 REGULAR"
                color = "orange"
            else:
                nivel = "🔴 MOROSO"
                color = "red"

        rows += f"""
        <tr>
            <td>{l['first_name']} {l['last_name']}</td>
            <td>{l.get('phone') or '-'}</td>
            <td style="color:{color};font-weight:bold">{nivel}</td>
            <td>{fmt_money(l.get('remaining', 0))}</td>
            <td>
                <a href="/loan/{l['loan_id']}"
                style="background:#1976d2;color:white;padding:6px 12px;border-radius:6px;text-decoration:none;">
                Ver préstamo
                </a>
            </td>
        </tr>
        """

    if not rows:
        rows = "<tr><td colspan='5'>Sin datos</td></tr>"

    body = f"""
    <div class="card">
        <h2>💳 Historial de Crédito</h2>

        <table>
            <thead>
                <tr>
                    <th>Cliente</th>
                    <th>Teléfono</th>
                    <th>Clasificación</th>
                    <th>Saldo</th>
                    <th>Acción</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
    """

    cur.close()
    conn.close()

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=current_user(),
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )

# ============================================================
# 🔎 CONSULTAR HISTORIAL POR CÉDULA
# ============================================================
@app.route("/bank/check-client", methods=["GET", "POST"])
@login_required
def check_client():

    result = ""

    if request.method == "POST":
        cedula = request.form.get("cedula")

        conn = get_conn()
        cur = conn.cursor()

        # buscar cliente por documento
        cur.execute("""
            SELECT id, first_name, last_name, phone
            FROM clients
            WHERE document_id = %s
        """, (cedula,))

        client = cur.fetchone()

        if not client:
            result = "<p style='color:red'>❌ Cliente no encontrado</p>"
        else:
            client_id = client["id"]

            # buscar préstamos activos del cliente
            cur.execute("""
                SELECT start_date, frequency, term_count,
                       COUNT(p.id) AS pagos
                FROM loans l
                LEFT JOIN payments p ON p.loan_id = l.id
                WHERE l.client_id = %s AND l.status='ACTIVO'
                GROUP BY l.id
            """, (client_id,))

            loans = cur.fetchall() or []
            today = date.today()

            estado = "🟢 BUENO"
            color = "green"

            for l in loans:
                start_date = l["start_date"]
                pagos = int(l["pagos"] or 0)
                term_count = int(l["term_count"] or 1)
                frequency = (l["frequency"] or "").lower()

                if frequency == "diario":
                    delta = timedelta(days=1)
                elif frequency == "semanal":
                    delta = timedelta(weeks=1)
                elif frequency == "quincenal":
                    delta = timedelta(days=14)
                else:
                    delta = timedelta(days=30)

                next_due = start_date + (delta * (pagos + 1))

                if today > next_due and pagos < term_count:
                    estado = "🔴 MOROSO"
                    color = "red"
                    break

            result = f"""
            <div style="margin-top:20px">
                <h3>{client['first_name']} {client['last_name']}</h3>
                <p>Tel: {client.get('phone') or '-'}</p>
                <h2 style="color:{color}">{estado}</h2>
            </div>
            """

        cur.close()
        conn.close()

    body = f"""
    <div class="card">
        <h2>🔎 Consultar Cliente por Cédula</h2>

        <form method="POST">
            <input name="cedula" placeholder="Ingrese cédula"
            style="padding:10px;width:250px;border-radius:6px;border:1px solid #ccc;">
            <button style="padding:10px 20px;background:#1976d2;color:white;border:none;border-radius:6px;">
            Buscar
            </button>
        </form>

        {result}
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=current_user(),
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )

# ============================================================
# ⚠️ CLIENTES EN RIESGO DE NO PAGAR (VERSIÓN CORREGIDA)
# ============================================================
from datetime import date, datetime, timedelta

@app.route("/bank/risk-clients")
@login_required
def risk_clients():

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            l.id AS loan_id,
            c.first_name,
            c.last_name,
            c.phone,
            l.start_date,
            l.frequency,
            l.term_count,
            l.remaining,
            COUNT(p.id) AS pagos_realizados
        FROM loans l
        JOIN clients c ON c.id = l.client_id
        LEFT JOIN payments p ON p.loan_id = l.id
        WHERE l.status = 'ACTIVO'
        GROUP BY
            l.id, c.first_name, c.last_name, c.phone,
            l.start_date, l.frequency, l.term_count, l.remaining
    """)

    loans = cur.fetchall() or []
    today = date.today()
    rows = ""

    for l in loans:

        start_date = l.get("start_date")
        if not start_date:
            continue

        # convertir fecha segura
        if isinstance(start_date, str):
            start_date = datetime.fromisoformat(start_date).date()
        elif hasattr(start_date, "date"):
            start_date = start_date.date()

        pagos = int(l.get("pagos_realizados") or 0)
        term_count = int(l.get("term_count") or 1)
        remaining = float(l.get("remaining") or 0)
        frequency = (l.get("frequency") or "").strip().lower()

        # =============================
        # INTERVALO SEGÚN FRECUENCIA
        # =============================
        if frequency == "diario":
            delta = timedelta(days=1)
        elif frequency == "semanal":
            delta = timedelta(weeks=1)
        elif frequency == "quincenal":
            delta = timedelta(days=14)
        else:
            delta = timedelta(days=30)

        next_due = start_date + (delta * (pagos + 1))
        days_late = (today - next_due).days if today > next_due else 0
        progreso = pagos / max(term_count, 1)

        # ====================================================
        # ⭐ LÓGICA PROFESIONAL DE RIESGO
        # ====================================================

        # CLIENTE NUEVO → NO EVALUAR
        if pagos < 3:
            riesgo = "🔵 EN OBSERVACIÓN"
            color = "blue"

        # RIESGO ALTO
        elif days_late > 7 or progreso < 0.2:
            riesgo = "🔴 ALTO"
            color = "red"

        # RIESGO MEDIO
        elif days_late > 0 or progreso < 0.5:
            riesgo = "🟡 MEDIO"
            color = "orange"

        # BAJO RIESGO
        else:
            riesgo = "🟢 BAJO"
            color = "green"

        rows += f"""
        <tr>
            <td>{l['first_name']} {l['last_name']}</td>
            <td>{l.get('phone') or '-'}</td>
            <td style="color:{color};font-weight:bold">{riesgo}</td>
            <td>{fmt_money(remaining)}</td>
            <td>
                <a href="/loan/{l['loan_id']}"
                style="background:#1976d2;color:white;padding:6px 12px;border-radius:6px;text-decoration:none;">
                Ver préstamo
                </a>
            </td>
        </tr>
        """

    if not rows:
        rows = "<tr><td colspan='5'>Sin clientes en riesgo</td></tr>"

    body = f"""
    <div class="card">
        <h2>⚠️ Clientes en Riesgo de No Pagar</h2>

        <table>
            <thead>
                <tr>
                    <th>Cliente</th>
                    <th>Teléfono</th>
                    <th>Nivel riesgo</th>
                    <th>Saldo</th>
                    <th>Acción</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>
    </div>
    """

    cur.close()
    conn.close()

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=current_user(),
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )



# ============================================================
# 📍 GUARDAR UBICACIÓN COBRADOR
# ============================================================
@app.route("/gps/update", methods=["POST"])
@login_required
def gps_update():

    lat = request.form.get("lat")
    lng = request.form.get("lng")

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO collector_location (user_id, latitude, longitude)
        VALUES (%s, %s, %s)
    """, (current_user()["id"], lat, lng))

    conn.commit()
    cur.close()
    conn.close()

    return "ok"

@app.route("/bank/collector-map")
@login_required
def collector_map():

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT latitude, longitude
        FROM collector_location
        ORDER BY updated_at DESC
        LIMIT 1
    """)

    loc = cur.fetchone()

    if not loc:
        return "Sin ubicación"

    lat = loc["latitude"]
    lng = loc["longitude"]

    body = f"""
    <h2>📍 Ubicación cobrador</h2>
    <iframe width="100%" height="500"
    src="https://maps.google.com/maps?q={lat},{lng}&z=15&output=embed"></iframe>
    """

    return render_template_string(TPL_LAYOUT, body=body)
	

# ============================================================
# 💰 COBRO SÁBADO (DÍA + ATRASADOS SOLO INFORMATIVO)
# ============================================================

from datetime import date, timedelta
from flask import render_template_string, get_flashed_messages
from psycopg2.extras import RealDictCursor

@app.route("/bank/cobro-sabado")
@login_required
def cobro_sabado():

    # 🔄 GENERAR ATRASOS AUTOMÁTICO
    generar_atrasos()

    user = current_user()

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    today = date.today()

    # =====================================================
    # 📅 CALCULAR PRÓXIMO SÁBADO
    # =====================================================
    days_ahead = 5 - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7

    proximo_sabado = today + timedelta(days=days_ahead)

    # =====================================================
    # FILTRO COBRADOR
    # =====================================================
    filtro_loans = ""
    params_hoy = [proximo_sabado]

    if user["role"] == "cobrador":
        filtro_loans = "AND created_by = %s"
        params_hoy.append(user["id"])

    # =====================================================
    # 💰 PRÉSTAMOS DEL SÁBADO (SOLO CLIENTES A COBRAR)
    # =====================================================
    cur.execute(f"""
        SELECT COUNT(*) AS prestamos,
               COALESCE(SUM(installment_amount),0) AS total
        FROM loans
        WHERE UPPER(status)='ACTIVO'
        AND next_payment_date = %s
        {filtro_loans}
    """, params_hoy)

    hoy = cur.fetchone()
    hoy_count = hoy["prestamos"]
    hoy_total = float(hoy["total"])

    # =====================================================
    # ⚠️ ATRASADOS (SOLO INFORMATIVO — NO SE SUMAN)
    # =====================================================
    params_atrasados = []

    if user["role"] == "cobrador":
        filtro_atrasado = "AND l.created_by = %s"
        params_atrasados.append(user["id"])
    else:
        filtro_atrasado = ""

    cur.execute(f"""
        SELECT COUNT(*) AS prestamos
        FROM loan_arrears la
        JOIN loans l ON l.id = la.loan_id
        WHERE la.paid = false
        {filtro_atrasado}
    """, params_atrasados)

    atrasado = cur.fetchone()
    atrasado_count = atrasado["prestamos"]

    # =====================================================
    # 📊 TOTAL A COBRAR HOY (SOLO LO DEL DÍA)
    # =====================================================
    total_count = hoy_count
    total_general = hoy_total

    cur.close()
    conn.close()

    # =====================================================
    # UI BODY CARD
    # =====================================================
    body = f"""
    <div class="card">

        <h2 style="text-align:center;">📅 Próximo Cobro del Sábado</h2>

        <h3 style="text-align:center;margin-top:10px;">
            {proximo_sabado.strftime("%d/%m/%Y")}
        </h3>

        <hr>

        <p style="font-size:22px;text-align:center;color:#16a34a;">
            ✔ {hoy_count} clientes para cobrar hoy
        </p>

        <hr>

        <h3 style="text-align:center;">💰 Total a Cobrar Hoy</h3>

        <p style="font-size:22px;text-align:center;">
            {total_count} clientes
        </p>

        <h1 style="font-size:48px;text-align:center;color:#16a34a;">
            {fmt_money(total_general)}
        </h1>

    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )
	
# ============================================================
# 💰 RESUMEN FINANCIERO REAL (BANCO REAL — CORREGIDO SIMPLE)
# ============================================================
@app.route("/bank/resumen")
@login_required
def bank_resumen():

    user = current_user()
    conn = get_conn()

    from psycopg2.extras import RealDictCursor
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # ============================
    # 💰 TOTAL PRESTADO
    # ============================
    cur.execute("""
        SELECT COALESCE(SUM(amount),0) AS total
        FROM loans
    """)
    total_prestado = float(cur.fetchone()["total"])

    # ============================
    # 💵 TOTAL COBRADO
    # ============================
    cur.execute("""
        SELECT COALESCE(SUM(amount),0) AS total
        FROM payments
        WHERE status <> 'ANULADO'
    """)
    total_cobrado = float(cur.fetchone()["total"])

    # ============================
    # 📈 INTERÉS GANADO
    # ============================
    cur.execute("""
        SELECT COALESCE(SUM(interest),0) AS total
        FROM payments
        WHERE status <> 'ANULADO'
    """)
    interes_ganado = float(cur.fetchone()["total"])

    # ============================
    # 📤 DINERO GASTADO REAL
    # ============================
    cur.execute("""
        SELECT COALESCE(SUM(amount),0) AS total
        FROM route_expenses
    """)
    dinero_gastado = float(cur.fetchone()["total"])

    # ============================
    # 🏦 BANCO REAL
    # ✔ préstamo resta (negativo)
    # ✔ descuento suma (positivo)
    # ✔ suma todo cash_reports
    # ============================
    cur.execute("""
        SELECT COALESCE(SUM(amount),0) AS total
        FROM cash_reports
    """)
    dinero_banco = float(cur.fetchone()["total"])

    cur.close()
    conn.close()

    # ============================
    # UI
    # ============================
    body = f"""
    <div class="card">
        <h2>🏦 Resumen Financiero</h2>

        <table style="width:100%;border-collapse:collapse">

            <tr>
                <td>💰 Total prestado</td>
                <td style="text-align:right"><b>{fmt_money(total_prestado)}</b></td>
            </tr>

            <tr>
                <td>💵 Total cobrado</td>
                <td style="text-align:right"><b>{fmt_money(total_cobrado)}</b></td>
            </tr>

            <tr>
                <td>📈 Interés ganado</td>
                <td style="text-align:right;color:#16a34a">
                    <b>{fmt_money(interes_ganado)}</b>
                </td>
            </tr>

            <tr>
                <td>🏦 Banco disponible</td>
                <td style="text-align:right;font-size:18px">
                    <b>{fmt_money(dinero_banco)}</b>
                </td>
            </tr>

            <tr>
                <td>📤 Dinero Gastado</td>
                <td style="text-align:right;color:#dc2626">
                    {fmt_money(dinero_gastado)}
                </td>
            </tr>

        </table>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        admin_whatsapp=ADMIN_WHATSAPP,
        app_brand=APP_BRAND,
        theme=get_theme()
    )


# ============================================================
# 🧾 CIERRE SEMANAL COMPLETO — JDM CASH NOW (FIX TOTAL)
# SOLO DINERO REAL EN CAJA
# ============================================================

@app.route("/bank/cierre-semanal")
@login_required
def cierre_semanal():

    from psycopg2.extras import RealDictCursor
    from datetime import date, timedelta

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # ===============================
        # ===============================
        # PERIODO DESDE ÚLTIMO CIERRE
        # ===============================
        cur.execute("""
            SELECT week_end
            FROM weekly_closures
            ORDER BY id DESC
            LIMIT 1
        """)

        ultimo_cierre = cur.fetchone()

        if ultimo_cierre:
            inicio = ultimo_cierre["week_end"] + timedelta(days=1)
        else:
            # si nunca han cerrado → empieza desde siempre
            inicio = date(2024,1,1)

        fin = date.today()
		
        # ===============================
        hoy = date.today()
        inicio = hoy - timedelta(days=hoy.weekday())
        fin = inicio + timedelta(days=6)

        # ===============================
        # PAGOS
        # ===============================
        cur.execute("""
            SELECT p.amount, c.first_name, c.last_name
            FROM payments p
            JOIN loans l ON l.id=p.loan_id
            JOIN clients c ON c.id=l.client_id
            WHERE DATE(p.date) BETWEEN %s AND %s
            AND COALESCE(p.status,'OK') <> 'ANULADO'
            ORDER BY p.date DESC
        """, (inicio, fin))
        pagos = cur.fetchall()

        # ===============================
        # PRÉSTAMOS ENTREGADOS
        # ===============================
        cur.execute("""
            SELECT l.amount, c.first_name, c.last_name
            FROM loans l
            JOIN clients c ON c.id=l.client_id
            WHERE l.start_date BETWEEN %s AND %s
            ORDER BY l.start_date DESC
        """, (inicio, fin))
        prestamos = cur.fetchall()

        # ===============================
        # GASTOS RUTA
        # ===============================
        cur.execute("""
            SELECT amount, note
            FROM route_expenses
            WHERE DATE(created_at) BETWEEN %s AND %s
            ORDER BY created_at DESC
        """, (inicio, fin))
        gastos = cur.fetchall()

        # ===============================
        # DESCUENTOS (SOLO INFORMATIVO)
        # ===============================
        cur.execute("""
            SELECT amount
            FROM initial_discounts
            WHERE DATE(created_at) BETWEEN %s AND %s
        """, (inicio, fin))
        descuentos = cur.fetchall()

    finally:
        cur.close()
        conn.close()

    # ===============================
    # TOTALES
    # ===============================
    total_pagos = sum(float(x["amount"]) for x in pagos)
    total_prestamos = sum(float(x["amount"]) for x in prestamos)
    total_gastos = sum(float(x["amount"]) for x in gastos)
    total_descuentos = sum(float(x["amount"]) for x in descuentos)

    # ⭐ BALANCE REAL (SOLO DINERO REAL)
    balance = total_pagos - total_prestamos - total_gastos

    # ===============================
    # HELPER HTML
    # ===============================
    def fila(txt, monto):
        return f"<tr><td>{txt}</td><td style='text-align:right'>{fmt_money(monto)}</td></tr>"

    pagos_html = "".join(
        fila(f"{p['first_name']} {p['last_name']}", p["amount"])
        for p in pagos
    ) or "<tr><td colspan=2>Sin pagos</td></tr>"

    prestamos_html = "".join(
        fila(f"{p['first_name']} {p['last_name']}", p["amount"])
        for p in prestamos
    ) or "<tr><td colspan=2>Sin préstamos</td></tr>"

    gastos_html = "".join(
        fila(g["note"] or "Gasto ruta", g["amount"])
        for g in gastos
    ) or "<tr><td colspan=2>Sin gastos</td></tr>"

    descuentos_html = "".join(
        fila("Descuento inicial", d["amount"])
        for d in descuentos
    ) or "<tr><td colspan=2>Sin descuentos</td></tr>"

    body = f"""
    <div class="card">

    <h2>🧾 Cierre semanal</h2>
    <b>Semana:</b> {inicio} → {fin}

    <hr>

    <h3>💰 Pagos</h3>
    <table>{pagos_html}</table>

    <h3>💸 Préstamos entregados</h3>
    <table>{prestamos_html}</table>

    <h3>📉 Descuentos (informativo)</h3>
    <table>{descuentos_html}</table>

    <h3>🚗 Gastos ruta</h3>
    <table>{gastos_html}</table>

    <hr>

    <h3>📊 Totales</h3>
    <table>
        {fila("Total cobrado", total_pagos)}
        {fila("Prestado", total_prestamos)}
        {fila("Gastos", total_gastos)}
        <tr>
            <td><b>Balance negocio</b></td>
            <td style="text-align:right"><b>{fmt_money(balance)}</b></td>
        </tr>
    </table>

    <br>

    <div style="display:flex;gap:10px">

        <form method="POST" action="/bank/cerrar-semana">
            <button style="background:#16a34a;color:white;padding:12px 18px;border:none;border-radius:8px;font-weight:700">
            ✅ CERRAR CUADRE
            </button>
        </form>

        <button onclick="window.print()" style="background:#2563eb;color:white;padding:12px 18px;border:none;border-radius:8px;font-weight:700">
        🖨️ IMPRIMIR 58mm
        </button>

    </div>

    </div>

    <style>
    @media print {{
        body {{ width:58mm;font-size:11px }}
        button {{ display:none }}
    }}
    </style>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=current_user(),
        flashes=get_flashed_messages(with_categories=True),
        app_brand=APP_BRAND,
        theme=get_theme()
    )


# ============================================================
# ✅ CERRAR SEMANA + GUARDAR EN HISTORIAL (FIX TOTAL)
# ============================================================

@app.route("/bank/cerrar-semana", methods=["POST"])
@login_required
def cerrar_semana():

    from datetime import date, timedelta
    from psycopg2.extras import RealDictCursor

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # ===============================
        # CREAR TABLA SI NO EXISTE
        # ===============================
        cur.execute("""
        CREATE TABLE IF NOT EXISTS weekly_closures (
            id SERIAL PRIMARY KEY,
            week_start DATE,
            week_end DATE,
            total_collected NUMERIC DEFAULT 0,
            total_loans NUMERIC DEFAULT 0,
            total_expenses NUMERIC DEFAULT 0,
            balance NUMERIC DEFAULT 0,
            closed_by VARCHAR(120),
            created_at TIMESTAMP DEFAULT NOW()
        )
        """)

        # ===============================
        # SEMANA ACTUAL
        # ===============================
        hoy = date.today()
        inicio = hoy - timedelta(days=hoy.weekday())
        fin = inicio + timedelta(days=6)

        # ===============================
        # TOTAL PAGOS
        # ===============================
        cur.execute("""
            SELECT COALESCE(SUM(amount),0) total
            FROM payments
            WHERE DATE(date) BETWEEN %s AND %s
            AND COALESCE(status,'OK') <> 'ANULADO'
        """, (inicio, fin))
        total_pagos = float(cur.fetchone()["total"])

        # ===============================
        # TOTAL PRESTAMOS
        # ===============================
        cur.execute("""
            SELECT COALESCE(SUM(amount),0) total
            FROM loans
            WHERE start_date BETWEEN %s AND %s
        """, (inicio, fin))
        total_prestamos = float(cur.fetchone()["total"])

        # ===============================
        # TOTAL GASTOS
        # ===============================
        cur.execute("""
            SELECT COALESCE(SUM(amount),0) total
            FROM route_expenses
            WHERE DATE(created_at) BETWEEN %s AND %s
        """, (inicio, fin))
        total_gastos = float(cur.fetchone()["total"])

        # ===============================
        # BALANCE REAL
        # ===============================
        balance = total_pagos - total_prestamos - total_gastos

        # ===============================
        # USUARIO SEGURO
        # ===============================
        user = current_user()
        usuario = "admin"

        if isinstance(user, dict):
            usuario = user.get("name", "admin")

        # ===============================
        # GUARDAR CIERRE
        # ===============================
        cur.execute("""
            INSERT INTO weekly_closures
            (week_start, week_end, total_collected,
             total_loans, total_expenses, balance, closed_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
        """, (
            inicio,
            fin,
            total_pagos,
            total_prestamos,
            total_gastos,
            balance,
            usuario
        ))

        conn.commit()
        flash("✅ Cuadre guardado en historial", "success")

    except Exception as e:
        conn.rollback()
        print("ERROR CIERRE:", e)  # ← ver error en logs
        flash(str(e), "danger")

    finally:
        cur.close()
        conn.close()

    return redirect("/bank/cierre-semanal")



# ============================================================
# 📚 HISTORIAL CIERRES SEMANALES + BORRAR SOLO ADMIN
# ============================================================

@app.route("/bank/historial-cierres")
@login_required
def historial_cierres():

    from psycopg2.extras import RealDictCursor

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT *
        FROM weekly_closures
        ORDER BY week_start DESC
    """)

    cierres = cur.fetchall()
    cur.close()
    conn.close()

    filas = ""

    for c in cierres:

        boton_borrar = ""

        # 🔐 SOLO ADMIN VE BOTÓN
        user = current_user()
        if user and user.get("role") == "admin":
            boton_borrar = f"""
            <form method="POST" action="/bank/borrar-cierre/{c['id']}"
            onsubmit="return confirm('¿Borrar este cierre?')">
                <button style="background:#dc2626;color:white;border:none;padding:6px 10px;border-radius:6px">
                🗑️
                </button>
            </form>
            """

        filas += f"""
        <tr>
            <td>{c['week_start']} → {c['week_end']}</td>
            <td>{fmt_money(c.get('total_collected',0))}</td>
            <td>{fmt_money(c.get('total_expenses',0))}</td>
            <td>{fmt_money(c.get('balance',0))}</td>
            <td>{c.get('closed_by')}</td>
            <td>{c['created_at']}</td>
            <td>{boton_borrar}</td>
        </tr>
        """

    body = f"""
    <div class="card">
    <h2>📚 Historial cierres</h2>

    <table>
        <tr>
            <th>Semana</th>
            <th>Cobrado</th>
            <th>Gastos</th>
            <th>Balance</th>
            <th>Usuario</th>
            <th>Fecha cierre</th>
            <th>Acción</th>
        </tr>
        {filas or "<tr><td colspan=7>Sin cierres</td></tr>"}
    </table>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=current_user(),
        flashes=get_flashed_messages(with_categories=True),
        app_brand=APP_BRAND,
        theme=get_theme()
    )
# ============================================================
# 💵 COBRAR CUOTA NORMAL DE PRÉSTAMO (MUEVE FECHA AUTOMÁTICA)
# ============================================================

@app.route("/bank/pagar/<int:loan_id>", methods=["POST"])
@login_required
def pagar_prestamo(loan_id):

    conn = get_conn()
    cur = conn.cursor()

    try:
        # OBTENER MONTO
        cur.execute("""
            SELECT installment_amount, frequency
            FROM loans
            WHERE id=%s
        """, (loan_id,))
        loan = cur.fetchone()

        if not loan:
            flash("Préstamo no encontrado", "danger")
            return redirect("/bank/cobro-sabado")

        monto = loan[0]
        frequency = loan[1]

        # GUARDAR PAGO
        cur.execute("""
            INSERT INTO payments (loan_id, amount)
            VALUES (%s,%s)
        """, (loan_id, monto))

        # MOVER FECHA
        cur.execute("""
            UPDATE loans
            SET next_payment_date =
                CASE frequency
                    WHEN 'diario' THEN next_payment_date + 1
                    WHEN 'semanal' THEN next_payment_date + 7
                    WHEN 'quincenal' THEN next_payment_date + 14
                    WHEN 'mensual' THEN (next_payment_date + INTERVAL '1 month')::date
                    ELSE next_payment_date + 7
                END
            WHERE id=%s
        """, (loan_id,))

        conn.commit()
        flash("✅ Pago registrado", "success")

    except Exception as e:
        conn.rollback()
        flash(str(e), "danger")

    finally:
        cur.close()
        conn.close()

    return redirect("/bank/cobro-sabado")

@app.route("/bank/borrar-cierre/<int:cierre_id>", methods=["POST"])
@login_required
def borrar_cierre(cierre_id):

    conn = get_conn()
    cur = conn.cursor()

    try:
        user = current_user()

        # 🔐 PROTECCIÓN REAL BACKEND
        if not user or user.get("role") != "admin":
            flash("⛔ Solo administrador puede borrar cierres", "danger")
            return redirect("/bank/historial-cierres")

        cur.execute("""
            DELETE FROM weekly_closures
            WHERE id=%s
        """, (cierre_id,))

        conn.commit()
        flash("✅ Cierre eliminado", "success")

    except Exception as e:
        conn.rollback()
        flash(str(e), "danger")

    finally:
        cur.close()
        conn.close()

    return redirect("/bank/historial-cierres")



# ============================================================
# 🏦 SISTEMA BANCO PRO — AJUSTE ADMIN + AUDITORIA + HISTORIAL (CORREGIDO)
# ============================================================

import os
from psycopg2.extras import RealDictCursor
from flask import request, redirect, flash, render_template_string, get_flashed_messages


# ============================================================
# 🔐 CODIGO ADMIN (USA VARIABLE DE ENTORNO EN RENDER)
# ============================================================
# En Render → Environment Variables → ADMIN_BANK_CODE=3128565688
ADMIN_BANK_CODE = os.getenv("ADMIN_BANK_CODE", "3128565688")


# ============================================================
# 🏦 OBTENER SALDO BANCO EN VIVO (REAL)
# ============================================================
def get_bank_balance():

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT COALESCE(SUM(amount),0) AS total
        FROM cash_reports
    """)

    row = cur.fetchone()
    balance = float(row["total"] or 0)

    cur.close()
    conn.close()

    return balance


# ============================================================
# 📜 REGISTRAR AUDITORIA
# ============================================================
def registrar_auditoria(user_id, accion, monto):

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO audit_log (
            user_id,
            action,
            details,
            created_at
        )
        VALUES (%s,%s,%s,NOW())
    """, (
        user_id,
        accion,
        f"Monto: {monto}"
    ))

    conn.commit()
    cur.close()
    conn.close()


# ============================================================
# 🏦 AJUSTAR BANCO ADMIN (CORREGIDO — PERMITE SIEMPRE)
# ============================================================
@app.route("/bank/agregar-dinero", methods=["GET", "POST"])
@login_required
def agregar_dinero_banco():

    user = current_user()

    try:

        # 🔒 SOLO ADMIN
        if user.get("role") != "admin":
            flash("⛔ Solo administrador puede hacer esto", "danger")
            return redirect("/dashboard")

        saldo_actual = get_bank_balance()

        # ============================
        # POST → AGREGAR DINERO
        # ============================
        if request.method == "POST":

            monto = request.form.get("monto", type=float)
            codigo = request.form.get("codigo")

            # VALIDAR MONTO
            if not monto or monto <= 0:
                flash("Monto inválido", "danger")
                return redirect(request.url)

            # VALIDAR CODIGO ADMIN
            if codigo != ADMIN_BANK_CODE:
                flash("Código administrador incorrecto", "danger")
                return redirect(request.url)

            conn = get_conn()
            cur = conn.cursor()

            # INSERT MOVIMIENTO BANCO
            cur.execute("""
                INSERT INTO cash_reports (
                    user_id,
                    movement_type,
                    amount,
                    ruta,
                    note,
                    created_at
                )
                VALUES (%s,'deposito_admin',%s,'Banco','Depósito administrador',NOW())
            """, (
                user["id"],
                abs(monto)
            ))

            conn.commit()
            cur.close()
            conn.close()

            # AUDITORIA
            registrar_auditoria(user["id"], "DEPOSITO_ADMIN_BANCO", monto)

            flash("✅ Dinero agregado al banco correctamente", "success")
            return redirect("/dashboard")

        # =============================
        # FORMULARIO
        # =============================
        body = f"""
        <div class="card" style="max-width:500px;margin:auto">

            <h2>🏦 Ajustar saldo banco</h2>

            <div style="
                background:#e0f2fe;
                padding:15px;
                border-radius:10px;
                margin-bottom:20px;">
                Saldo actual: <b>{fmt_money(saldo_actual)}</b>
            </div>

            <div style="
                background:#fff7ed;
                padding:15px;
                border-radius:10px;
                margin-bottom:20px;">
                ℹ️ Puedes agregar dinero para ajustar el saldo del banco
            </div>

            <form method="post">

                <label>Monto a agregar</label>
                <input type="number" step="0.01" name="monto" required>

                <label>Código administrador</label>
                <input type="password" name="codigo" required>

                <button class="btn btn-primary">
                    Agregar dinero
                </button>

            </form>
        </div>
        """

        return render_template_string(
            TPL_LAYOUT,
            body=body,
            user=user,
            flashes=get_flashed_messages(with_categories=True),
            app_brand=APP_BRAND,
            theme=get_theme()
        )

    except Exception as e:
        flash(f"Error del sistema: {str(e)}", "danger")
        return redirect("/dashboard")


# ============================================================
# 📊 VER HISTORIAL DEPOSITOS ADMIN
# ============================================================
@app.route("/bank/historial-depositos")
@login_required
def historial_depositos():

    user = current_user()

    if user.get("role") != "admin":
        flash("Solo admin", "danger")
        return redirect("/dashboard")

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT cr.*, u.username
        FROM cash_reports cr
        LEFT JOIN users u ON u.id = cr.user_id
        WHERE cr.movement_type='deposito_admin'
        ORDER BY cr.created_at DESC
        LIMIT 100
    """)

    rows = cur.fetchall()

    cur.close()
    conn.close()

    if not rows:
        body = """
        <div class="card">
            <h2>Historial depósitos admin</h2>
            <p>No hay registros</p>
        </div>
        """
    else:
        trs = ""
        for r in rows:
            trs += f"""
            <tr>
                <td>{r["created_at"]}</td>
                <td>{r.get("username","Admin")}</td>
                <td>{fmt_money(r["amount"])}</td>
                <td>{r["note"]}</td>
            </tr>
            """

        body = f"""
        <div class="card">
            <h2>Historial depósitos admin</h2>

            <table>
                <tr>
                    <th>Fecha</th>
                    <th>Usuario</th>
                    <th>Monto</th>
                    <th>Nota</th>
                </tr>
                {trs}
            </table>
        </div>
        """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=user,
        flashes=get_flashed_messages(with_categories=True),
        app_brand=APP_BRAND,
        theme=get_theme()
    )


# ============================================================
#  LOGOUT Y RECUPERAR PASSWORD
# ============================================================

@app.route("/logout")
def logout():
    session.clear()
    flash("Sesión cerrada.", "success")
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        flash("Contacte al admin para recuperar su contraseña.", "info")
        return redirect(url_for("login"))

    body = """
    <div class="card">
      <h2>Recuperar contraseña</h2>
      <p>Por seguridad, la recuperación se realiza por el administrador.</p>
      <p>Escríbele por WhatsApp al número mostrado en la parte superior (SOS).</p>
      <form method="post">
        <button class="btn btn-primary">Entendido</button>
      </form>
    </div>
    """

    return render_template_string(
        TPL_LAYOUT,
        body=body,
        user=current_user(),
        app_brand=APP_BRAND,
        admin_whatsapp=ADMIN_WHATSAPP,
        flashes=get_flashed_messages(with_categories=True),
        theme=get_theme()
    )
# ============================================================
# 🏠 INDEX — REDIRECCIÓN ÚNICA
# ============================================================
@app.route("/")
@login_required
def index():
    return redirect(url_for("dashboard"))

# ============================================================
# 🔧 CREAR ADMIN FORZADO (SOLO EMERGENCIA)
# ============================================================
@app.route("/admin-force-create")
def admin_force_create():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("DELETE FROM users;")

    cur.execute("""
        INSERT INTO users (username, password_hash, role, created_at)
        VALUES (%s, %s, %s, %s)
    """, (
        "admin",
        generate_password_hash("admin"),
        "admin",
        datetime.utcnow()
    ))

    conn.commit()
    cur.close()
    conn.close()

    return "✅ ADMIN CREADO → usuario: admin | contraseña: admin"

# ==========================================
# 🗑 ELIMINAR PAGO ADELANTADO
# ==========================================
@app.route("/advance/delete/<int:payment_id>", methods=["POST"])
@login_required
def delete_advance(payment_id):

    conn = get_conn()
    cur = conn.cursor()

    try:
        # obtener info del pago
        cur.execute("""
            SELECT loan_id, weeks_advanced
            FROM payments
            WHERE id=%s
        """, (payment_id,))
        pay = cur.fetchone()

        if not pay:
            flash("Pago no encontrado", "danger")
            return redirect("/bank/advance")

        loan_id = pay["loan_id"]
        weeks = pay["weeks_advanced"] or 1

        # borrar pago
        cur.execute("DELETE FROM payments WHERE id=%s", (payment_id,))

        # devolver semanas al calendario del préstamo
        cur.execute("""
            UPDATE loans
            SET next_payment_date =
                next_payment_date - (%s * INTERVAL '7 day')
            WHERE id=%s
        """, (weeks, loan_id))

        conn.commit()
        flash("Pago adelantado eliminado", "success")

    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")

    cur.close()
    conn.close()

    return redirect("/bank/advance")



# ============================================================
#  FINAL – EJECUCIÓN
# ============================================================

# Inicializar BD al importar (sirve para Flask 3 + Render, sin before_first_request)
# init_db()


if __name__ == "__main__":
    print("[JDM Cash Now] Iniciando servidor…")
    app.run(host="0.0.0.0", port=5000, debug=True)
















