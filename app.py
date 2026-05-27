import os, uuid, io, json, threading, time, base64, urllib.request, urllib.error
from datetime import datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
load_dotenv()  # charge le fichier .env en local, ignoré en production

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, send_file, abort, session as flask_session)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    PDF_OK = True
except ImportError:
    PDF_OK = False

app = Flask(__name__)

# Base de données : PostgreSQL en production, SQLite en local
# On supprime TOUS les espaces/newlines autour de l'URL (copie Render parfois pollue)
_db_url = ''.join((os.environ.get('DATABASE_URL') or '').split()) or 'sqlite:///agenda.db'
# Normalise le préfixe pour psycopg3 (driver Python 3.14 compatible)
if _db_url.startswith('postgres://'):
    _db_url = _db_url.replace('postgres://', 'postgresql+psycopg://', 1)
elif _db_url.startswith('postgresql://'):
    _db_url = _db_url.replace('postgresql://', 'postgresql+psycopg://', 1)

# SQLite : désactive les vérifications de thread pour compatibilité Gunicorn
_connect_args = {'check_same_thread': False} if _db_url.startswith('sqlite') else {}

app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY', 'changez-cette-cle-en-production'),
    SQLALCHEMY_DATABASE_URI=_db_url,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={'connect_args': _connect_args},
)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Veuillez vous connecter.'
login_manager.login_message_category = 'warning'

# ─── MODÈLES ──────────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    nom = db.Column(db.String(100))
    email = db.Column(db.String(120))
    is_admin = db.Column(db.Boolean, default=True)

    def set_password(self, p): self.password_hash = generate_password_hash(p)
    def check_password(self, p): return check_password_hash(self.password_hash, p)


class Parametre(db.Model):
    __tablename__ = 'parametres'
    id = db.Column(db.Integer, primary_key=True)
    cle = db.Column(db.String(100), unique=True, nullable=False)
    valeur = db.Column(db.Text)


class Client(db.Model):
    __tablename__ = 'clients'
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(20), unique=True)
    nom = db.Column(db.String(100), nullable=False)
    prenom = db.Column(db.String(100))
    email = db.Column(db.String(120))
    telephone = db.Column(db.String(20))
    telephone2 = db.Column(db.String(20))
    adresse = db.Column(db.Text)
    ville = db.Column(db.String(100))
    code_postal = db.Column(db.String(10))
    type_client = db.Column(db.String(20), default='particulier')
    societe = db.Column(db.String(150))
    siret_client = db.Column(db.String(20))
    notes = db.Column(db.Text)
    actif = db.Column(db.Boolean, default=True)
    latitude = db.Column(db.Float)   # coordonnées GPS (géocodage automatique)
    longitude = db.Column(db.Float)
    access_token = db.Column(db.String(36), unique=True, default=lambda: str(uuid.uuid4()))
    portal_password_hash = db.Column(db.String(256))
    portal_actif = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    interventions = db.relationship('Intervention', backref='client', lazy=True,
                                    cascade='all, delete-orphan')

    @property
    def nom_complet(self):
        return f"{self.prenom} {self.nom}".strip() if self.prenom else self.nom

    @property
    def nom_affichage(self):
        if self.type_client == 'professionnel' and self.societe:
            return self.societe
        return self.nom_complet

    def set_portal_password(self, p): self.portal_password_hash = generate_password_hash(p)
    def check_portal_password(self, p):
        return check_password_hash(self.portal_password_hash, p) if self.portal_password_hash else False


class Intervention(db.Model):
    __tablename__ = 'interventions'
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(20), unique=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    titre = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    type_intervention = db.Column(db.String(100))
    priorite = db.Column(db.String(20), default='normale')
    statut = db.Column(db.String(20), default='planifiee')
    date_planifiee = db.Column(db.DateTime, nullable=False)
    duree_estimee = db.Column(db.Integer, default=60)
    technicien = db.Column(db.String(100))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    bon = db.relationship('BonIntervention', backref='intervention', uselist=False,
                          cascade='all, delete-orphan')

    @property
    def couleur(self):
        if self.priorite == 'urgente': return '#e74c3c'
        # Couleur par type de prestation (du plus spécifique au moins spécifique)
        t = (self.type_intervention or '').lower()
        if 'dératisation' in t and 'désinsectisation' in t and 'désinfection' in t:
            return '#c0392b'   # rouge foncé — triple prestation
        if 'dératisation' in t and 'désinsectisation' in t:
            return '#8e44ad'   # violet — double prestation
        if 'dératisation' in t:
            return '#e67e22'   # orange
        if 'désinsectisation' in t:
            return '#1aabe3'   # bleu HPS
        if 'désinfection' in t:
            return '#27ae60'   # vert
        return {'planifiee': '#3788d8', 'en_cours': '#f39c12',
                'terminee': '#95a5a6', 'annulee': '#bdc3c7'}.get(self.statut, '#3788d8')

    @property
    def statut_label(self):
        return {'planifiee': 'Planifiée', 'en_cours': 'En cours',
                'terminee': 'Terminée', 'annulee': 'Annulée'}.get(self.statut, self.statut)


class BonIntervention(db.Model):
    __tablename__ = 'bons_intervention'
    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String(20), unique=True, nullable=False)
    intervention_id = db.Column(db.Integer, db.ForeignKey('interventions.id'), nullable=False)
    travaux_effectues = db.Column(db.Text)
    materiaux_utilises = db.Column(db.Text)
    temps_passe = db.Column(db.Integer)
    observations = db.Column(db.Text)
    recommandations = db.Column(db.Text)
    statut = db.Column(db.String(20), default='brouillon')
    date_creation = db.Column(db.DateTime, default=datetime.utcnow)
    date_finalisation = db.Column(db.DateTime)
    date_envoi = db.Column(db.DateTime)
    signature_client = db.Column(db.Boolean, default=False)
    date_signature = db.Column(db.DateTime)

    @property
    def statut_label(self):
        return {'brouillon': 'Brouillon', 'finalise': 'Finalisé',
                'envoye': 'Envoyé', 'signe': 'Signé'}.get(self.statut, self.statut)

    @property
    def statut_couleur(self):
        return {'brouillon': 'secondary', 'finalise': 'primary',
                'envoye': 'info', 'signe': 'success'}.get(self.statut, 'secondary')


class BonPhoto(db.Model):
    __tablename__ = 'bon_photos'
    id = db.Column(db.Integer, primary_key=True)
    bon_id = db.Column(db.Integer, db.ForeignKey('bons_intervention.id'), nullable=False)
    nom = db.Column(db.String(200), default='photo')
    data = db.Column(db.Text, nullable=False)   # image encodée en base64
    mimetype = db.Column(db.String(50), default='image/jpeg')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    bon = db.relationship('BonIntervention', backref='photos')


class ContratClient(db.Model):
    __tablename__ = 'contrats_clients'
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    type_prestation = db.Column(db.String(100), nullable=False)
    passages_annuels = db.Column(db.Integer, default=1)
    date_debut = db.Column(db.Date)
    date_fin = db.Column(db.Date)
    actif = db.Column(db.Boolean, default=True)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    client = db.relationship('Client', backref='contrats')

    def passages_realises(self, annee=None):
        annee = annee or datetime.now().year
        return Intervention.query.filter(
            Intervention.client_id == self.client_id,
            Intervention.type_intervention.ilike(f'%{self.type_prestation}%'),
            db.func.extract('year', Intervention.date_planifiee) == annee,
            Intervention.statut == 'terminee'
        ).count()

    def passages_restants(self, annee=None):
        return max(0, self.passages_annuels - self.passages_realises(annee))

    def pct_realises(self, annee=None):
        if not self.passages_annuels: return 0
        return min(100, int(self.passages_realises(annee) * 100 / self.passages_annuels))

# ─── HELPERS ──────────────────────────────────────────────────────────────────

@login_manager.user_loader
def load_user(uid): return User.query.get(int(uid))

def get_param(cle, defaut=''):
    # Priorité : variable d'environnement > base de données > valeur par défaut
    env_key = 'APP_' + cle.upper()
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val
    p = Parametre.query.filter_by(cle=cle).first()
    return p.valeur if p else defaut

def set_param(cle, valeur):
    p = Parametre.query.filter_by(cle=cle).first()
    if p: p.valeur = valeur
    else: db.session.add(Parametre(cle=cle, valeur=valeur))
    db.session.commit()

def next_ref(model, prefix, pad=5):
    last = model.query.order_by(model.id.desc()).first()
    return f"{prefix}{((last.id if last else 0) + 1):0{pad}d}"

def next_bon_num():
    last = BonIntervention.query.order_by(BonIntervention.id.desc()).first()
    n = (last.id if last else 0) + 1
    return f"BI{datetime.now().year}{n:04d}"

def generer_pdf(bon):
    if not PDF_OK:
        raise RuntimeError("ReportLab non installé.")
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=2*cm, leftMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    styles = getSampleStyleSheet()
    s_titre = ParagraphStyle('t', parent=styles['Title'], fontSize=15,
                             alignment=TA_CENTER, spaceAfter=6)
    s_h = ParagraphStyle('h', parent=styles['Normal'], fontSize=10,
                         fontName='Helvetica-Bold', spaceAfter=4)
    s_n = ParagraphStyle('n', parent=styles['Normal'], fontSize=9, spaceAfter=3)
    s_sm = ParagraphStyle('sm', parent=styles['Normal'], fontSize=8,
                          alignment=TA_CENTER, textColor=colors.grey)

    soc = get_param('societe', 'Ma Société')
    inter = bon.intervention
    cli = inter.client
    elems = []

    elems.append(Paragraph(soc, s_titre))
    info_soc = ' | '.join(filter(None, [
        get_param('adresse'), get_param('telephone'), get_param('email')]))
    if info_soc: elems.append(Paragraph(info_soc, ParagraphStyle(
        'si', parent=styles['Normal'], fontSize=8, alignment=TA_CENTER, spaceAfter=4)))
    if get_param('siret'):
        elems.append(Paragraph(f"SIRET : {get_param('siret')}", ParagraphStyle(
            'si2', parent=styles['Normal'], fontSize=8, alignment=TA_CENTER, spaceAfter=10)))

    elems.append(Paragraph(f"BON D'INTERVENTION N° {bon.numero}", s_titre))
    elems.append(Spacer(1, 0.4*cm))

    data_h = [
        ['', 'INTERVENTION', '', 'CLIENT', ''],
        ['Réf.', inter.reference, '', 'Nom', cli.nom_affichage],
        ['Date', inter.date_planifiee.strftime('%d/%m/%Y %H:%M'), '',
         'Email', cli.email or ''],
        ['Type', inter.type_intervention or '', '', 'Tél', cli.telephone or ''],
        ['Tech.', inter.technicien or '', '',
         'Adresse', f"{cli.adresse or ''} {cli.code_postal or ''} {cli.ville or ''}".strip()],
        ['Statut', inter.statut_label, '', '', ''],
    ]
    t = Table(data_h, colWidths=[2*cm, 6*cm, 0.3*cm, 2.5*cm, 6.7*cm])
    t.setStyle(TableStyle([
        ('SPAN', (0, 0), (1, 0)), ('SPAN', (3, 0), (4, 0)),
        ('BACKGROUND', (0, 0), (1, 0), colors.HexColor('#2c3e50')),
        ('BACKGROUND', (3, 0), (4, 0), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, 0), (4, 0), colors.white),
        ('FONTNAME', (0, 0), (4, 0), 'Helvetica-Bold'),
        ('FONTNAME', (0, 1), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME', (3, 1), (3, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('GRID', (0, 0), (1, -1), 0.4, colors.grey),
        ('GRID', (3, 0), (4, -1), 0.4, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('PADDING', (0, 0), (-1, -1), 4),
        ('ROWBACKGROUNDS', (0, 1), (1, -1), [colors.white, colors.HexColor('#f5f5f5')]),
        ('ROWBACKGROUNDS', (3, 1), (4, -1), [colors.white, colors.HexColor('#f5f5f5')]),
    ]))
    elems.append(t)
    elems.append(Spacer(1, 0.5*cm))

    if inter.description:
        elems.append(Paragraph("Description :", s_h))
        elems.append(Paragraph(inter.description.replace('\n', '<br/>'), s_n))
        elems.append(Spacer(1, 0.3*cm))

    elems.append(Paragraph("Travaux effectués :", s_h))
    elems.append(Paragraph((bon.travaux_effectues or 'À compléter').replace('\n', '<br/>'), s_n))
    elems.append(Spacer(1, 0.3*cm))

    if bon.materiaux_utilises:
        try:
            mats = json.loads(bon.materiaux_utilises)
            if mats:
                elems.append(Paragraph("Matériaux utilisés :", s_h))
                md = [['Désignation', 'Qté', 'Unité']]
                for m in mats:
                    md.append([m.get('designation',''), str(m.get('quantite','')), m.get('unite','')])
                tm = Table(md, colWidths=[10*cm, 2.5*cm, 5*cm])
                tm.setStyle(TableStyle([
                    ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#34495e')),
                    ('TEXTCOLOR', (0,0), (-1,0), colors.white),
                    ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0,0), (-1,-1), 9),
                    ('GRID', (0,0), (-1,-1), 0.4, colors.grey),
                    ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f5f5f5')]),
                    ('PADDING', (0,0), (-1,-1), 4),
                ]))
                elems.append(tm)
                elems.append(Spacer(1, 0.3*cm))
        except Exception:
            pass

    if bon.temps_passe:
        h, m = divmod(bon.temps_passe, 60)
        elems.append(Paragraph(f"Temps passé : {h}h{m:02d}" if h else f"Temps passé : {m} min", s_n))
        elems.append(Spacer(1, 0.2*cm))

    if bon.observations:
        elems.append(Paragraph("Observations :", s_h))
        elems.append(Paragraph(bon.observations.replace('\n', '<br/>'), s_n))
        elems.append(Spacer(1, 0.3*cm))

    if bon.recommandations:
        elems.append(Paragraph("Recommandations :", s_h))
        elems.append(Paragraph(bon.recommandations.replace('\n', '<br/>'), s_n))
        elems.append(Spacer(1, 0.3*cm))

    elems.append(Spacer(1, 0.8*cm))
    sig = [
        ['Signature du technicien', 'Signature du client'],
        ['', ''],
        ['', ''],
        ['Nom & Date :', 'Nom & Date :'],
    ]
    ts = Table(sig, colWidths=[8.75*cm, 8.75*cm])
    ts.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('BOX', (0,0), (0,-1), 1, colors.black),
        ('BOX', (1,0), (1,-1), 1, colors.black),
        ('LINEBELOW', (0,0), (-1,0), 0.5, colors.grey),
        ('ROWHEIGHT', (0,1), (-1,2), 55),
        ('PADDING', (0,0), (-1,-1), 6),
    ]))
    elems.append(ts)

    elems.append(Spacer(1, 0.4*cm))
    elems.append(Paragraph(
        f"Document établi le {bon.date_creation.strftime('%d/%m/%Y')} — {bon.numero} — {soc}",
        s_sm))

    doc.build(elems)
    buf.seek(0)
    return buf


def envoyer_bon_email(bon):
    api_key = ''.join(os.environ.get('APP_BREVO_API_KEY', '').split())
    if not api_key:
        return False, "Clé API Brevo manquante. Ajoutez APP_BREVO_API_KEY dans les variables d'environnement Render."
    cli = bon.intervention.client
    if not cli.email:
        return False, "Le client n'a pas d'adresse email."
    soc = get_param('societe', 'Ma Société')
    sender_email = get_param('mail_username') or get_param('email')
    if not sender_email:
        return False, "Email expéditeur manquant dans les Paramètres (champ 'Email expéditeur')."
    corps = (f"Bonjour {cli.prenom or cli.nom},\n\n"
             f"Veuillez trouver ci-joint votre bon d'intervention N° {bon.numero} "
             f"du {bon.intervention.date_planifiee.strftime('%d/%m/%Y')}.\n\n"
             f"Cordialement,\n{soc}\n{get_param('telephone')}\n{get_param('email')}")
    try:
        pdf_buf = generer_pdf(bon)
        pdf_b64 = base64.b64encode(pdf_buf.read()).decode('utf-8')
    except Exception as e:
        return False, f"Erreur PDF : {e}"
    payload = json.dumps({
        "sender": {"name": soc, "email": sender_email},
        "to": [{"email": cli.email, "name": cli.nom_affichage}],
        "subject": f"Bon d'intervention N° {bon.numero} — {soc}",
        "textContent": corps,
        "attachment": [{"content": pdf_b64, "name": f"bon_{bon.numero}.pdf"}]
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.brevo.com/v3/smtp/email',
        data=payload,
        headers={'api-key': api_key, 'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status in (200, 201, 202):
                return True, "Email envoyé avec succès via Brevo."
            return False, f"Erreur Brevo : statut {resp.status}"
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='ignore')[:300]
        return False, f"Erreur Brevo {e.code} : {detail}"
    except Exception as e:
        return False, f"Erreur envoi : {e}"

# ─── GÉOCODAGE & OPTIMISATION TOURNÉE ────────────────────────────────────────

def _geocoder_client(client):
    """Géocode l'adresse d'un client via Nominatim (OpenStreetMap). Met à jour lat/lng."""
    if client.latitude and client.longitude:
        return True  # déjà géocodé
    adresse = ' '.join(filter(None, [client.adresse, client.code_postal, client.ville, 'France']))
    try:
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/search?q={urllib.request.quote(adresse)}&format=json&limit=1",
            headers={'User-Agent': 'AgendaHPS/1.0'}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            if data:
                client.latitude = float(data[0]['lat'])
                client.longitude = float(data[0]['lon'])
                db.session.commit()
                return True
    except Exception:
        pass
    return False

def _haversine(lat1, lon1, lat2, lon2):
    """Distance à vol d'oiseau en km entre deux points GPS."""
    import math
    R = 6371
    d = math.radians
    a = (math.sin((d(lat2)-d(lat1))/2)**2 +
         math.cos(d(lat1)) * math.cos(d(lat2)) * math.sin((d(lon2)-d(lon1))/2)**2)
    return R * 2 * math.asin(math.sqrt(a))

def _optimiser_tournee(points, depart_lat=None, depart_lon=None):
    """Algorithme du plus proche voisin (TSP heuristique)."""
    if not points:
        return []
    remaining = list(points)
    route = []
    # Point de départ : adresse fournie ou premier point de la liste
    if depart_lat and depart_lon:
        cur_lat, cur_lon = depart_lat, depart_lon
    else:
        first = remaining.pop(0)
        route.append(first)
        cur_lat, cur_lon = first['lat'], first['lon']
    while remaining:
        nearest = min(remaining, key=lambda p: _haversine(cur_lat, cur_lon, p['lat'], p['lon']))
        route.append(nearest)
        cur_lat, cur_lon = nearest['lat'], nearest['lon']
        remaining.remove(nearest)
    return route

# ─── ROUTES AUTH ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return redirect(url_for('dashboard') if current_user.is_authenticated else url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        u = User.query.filter_by(username=request.form.get('username')).first()
        if u and u.check_password(request.form.get('password', '')):
            login_user(u, remember='remember' in request.form)
            flash(f'Bienvenue, {u.nom or u.username} !', 'success')
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Identifiant ou mot de passe incorrect.', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Déconnexion réussie.', 'info')
    return redirect(url_for('login'))

# ─── DASHBOARD ────────────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    today = datetime.now().date()
    stats = dict(
        clients=Client.query.filter_by(actif=True).count(),
        auj=Intervention.query.filter(
            db.func.date(Intervention.date_planifiee) == today,
            Intervention.statut.in_(['planifiee', 'en_cours'])).count(),
        semaine=Intervention.query.filter(
            Intervention.date_planifiee.between(datetime.now(),
                                                datetime.now() + timedelta(days=7)),
            Intervention.statut.in_(['planifiee', 'en_cours'])).count(),
        bons_brouillon=BonIntervention.query.filter_by(statut='brouillon').count(),
        urgentes=Intervention.query.filter_by(priorite='urgente', statut='planifiee').count(),
    )
    prochaines = Intervention.query.filter(
        Intervention.date_planifiee >= datetime.now(),
        Intervention.statut.in_(['planifiee', 'en_cours'])
    ).order_by(Intervention.date_planifiee).limit(8).all()
    return render_template('dashboard.html', stats=stats, prochaines=prochaines)

# ─── CLIENTS ──────────────────────────────────────────────────────────────────

@app.route('/clients')
@login_required
def clients_liste():
    q = request.args.get('q', '')
    query = Client.query
    if q:
        query = query.filter(db.or_(
            Client.nom.ilike(f'%{q}%'), Client.prenom.ilike(f'%{q}%'),
            Client.email.ilike(f'%{q}%'), Client.telephone.ilike(f'%{q}%'),
            Client.societe.ilike(f'%{q}%'), Client.reference.ilike(f'%{q}%'),
        ))
    clients = query.order_by(Client.nom).all()
    return render_template('clients/index.html', clients=clients, q=q)

@app.route('/clients/nouveau', methods=['GET', 'POST'])
@login_required
def client_nouveau():
    if request.method == 'POST':
        c = Client(
            reference=next_ref(Client, 'CLT'),
            nom=request.form['nom'].strip(),
            prenom=request.form.get('prenom','').strip(),
            email=request.form.get('email','').strip(),
            telephone=request.form.get('telephone','').strip(),
            telephone2=request.form.get('telephone2','').strip(),
            adresse=request.form.get('adresse','').strip(),
            ville=request.form.get('ville','').strip(),
            code_postal=request.form.get('code_postal','').strip(),
            type_client=request.form.get('type_client','particulier'),
            societe=request.form.get('societe','').strip(),
            siret_client=request.form.get('siret_client','').strip(),
            notes=request.form.get('notes','').strip(),
        )
        db.session.add(c)
        db.session.commit()

        if request.form.get('planif_auto') and request.form.get('date_inter'):
            try:
                di = datetime.strptime(request.form['date_inter'], '%Y-%m-%dT%H:%M')
                inter = Intervention(
                    reference=next_ref(Intervention, 'INT'),
                    client_id=c.id,
                    titre=request.form.get('titre_inter', f'Première intervention — {c.nom_affichage}'),
                    description=request.form.get('desc_inter','').strip(),
                    type_intervention=request.form.get('type_inter','').strip(),
                    priorite=request.form.get('priorite_inter','normale'),
                    date_planifiee=di,
                    duree_estimee=int(request.form.get('duree_inter', 60) or 60),
                    technicien=request.form.get('technicien_inter','').strip(),
                )
                db.session.add(inter)
                db.session.commit()
                flash(f'Client {c.nom_affichage} créé + intervention planifiée ({inter.reference}).', 'success')
            except Exception as e:
                flash(f'Client créé, erreur planification : {e}', 'warning')
        else:
            flash(f'Client {c.nom_affichage} créé — Réf. {c.reference}', 'success')
        return redirect(url_for('client_detail', id=c.id))

    techniciens = get_param('techniciens', '')
    types_inter = get_param('types_intervention', '')
    return render_template('clients/create.html', client=None,
                           techniciens=techniciens, types_inter=types_inter)

@app.route('/clients/<int:id>', methods=['GET'])
@login_required
def client_detail(id):
    c = Client.query.get_or_404(id)
    interventions = Intervention.query.filter_by(client_id=id)\
        .order_by(Intervention.date_planifiee.desc()).all()
    base = get_param('base_url', request.host_url.rstrip('/'))
    lien = f"{base}/portail/{c.access_token}"
    annee = datetime.now().year
    return render_template('clients/detail.html', client=c,
                           interventions=interventions, lien_portail=lien,
                           types_prestation=TYPES_PRESTATION, annee=annee)

@app.route('/clients/<int:id>/modifier', methods=['GET', 'POST'])
@login_required
def client_modifier(id):
    c = Client.query.get_or_404(id)
    if request.method == 'POST':
        c.nom = request.form['nom'].strip()
        c.prenom = request.form.get('prenom','').strip()
        c.email = request.form.get('email','').strip()
        c.telephone = request.form.get('telephone','').strip()
        c.telephone2 = request.form.get('telephone2','').strip()
        c.adresse = request.form.get('adresse','').strip()
        c.ville = request.form.get('ville','').strip()
        c.code_postal = request.form.get('code_postal','').strip()
        c.type_client = request.form.get('type_client','particulier')
        c.societe = request.form.get('societe','').strip()
        c.siret_client = request.form.get('siret_client','').strip()
        c.notes = request.form.get('notes','').strip()
        db.session.commit()
        flash('Fiche client mise à jour.', 'success')
        return redirect(url_for('client_detail', id=id))
    techniciens = get_param('techniciens', '')
    types_inter = get_param('types_intervention', '')
    return render_template('clients/create.html', client=c,
                           techniciens=techniciens, types_inter=types_inter)

@app.route('/clients/<int:id>/supprimer', methods=['POST'])
@login_required
def client_supprimer(id):
    c = Client.query.get_or_404(id)
    c.actif = False
    db.session.commit()
    flash(f'Client {c.nom_affichage} désactivé.', 'info')
    return redirect(url_for('clients_liste'))

@app.route('/clients/<int:id>/portail/activer', methods=['POST'])
@login_required
def portail_activer(id):
    c = Client.query.get_or_404(id)
    if not c.email:
        flash('Le client doit avoir une adresse email.', 'warning')
    else:
        c.portal_actif = True
        if not c.access_token:
            c.access_token = str(uuid.uuid4())
        db.session.commit()
        flash('Portail client activé. Partagez le lien avec le client.', 'success')
    return redirect(url_for('client_detail', id=id))

@app.route('/clients/<int:id>/portail/desactiver', methods=['POST'])
@login_required
def portail_desactiver(id):
    c = Client.query.get_or_404(id)
    c.portal_actif = False
    db.session.commit()
    flash('Portail client désactivé.', 'info')
    return redirect(url_for('client_detail', id=id))

@app.route('/clients/<int:id>/portail/regenerer', methods=['POST'])
@login_required
def portail_regenerer(id):
    c = Client.query.get_or_404(id)
    c.access_token = str(uuid.uuid4())
    c.portal_password_hash = None
    db.session.commit()
    flash('Lien régénéré. L\'ancien lien est invalidé.', 'success')
    return redirect(url_for('client_detail', id=id))

# ─── AGENDA / INTERVENTIONS ───────────────────────────────────────────────────

@app.route('/agenda')
@login_required
def agenda():
    return render_template('interventions/agenda.html')

@app.route('/agenda/api/events')
@login_required
def agenda_events():
    start = request.args.get('start', '')
    end   = request.args.get('end', '')
    q = Intervention.query
    try:
        # FullCalendar envoie des dates ISO avec timezone ex: 2026-05-24T00:00:00+02:00
        # On extrait juste YYYY-MM-DD pour éviter les problèmes de comparaison
        if start:
            start_dt = datetime.strptime(start[:10], '%Y-%m-%d')
            q = q.filter(Intervention.date_planifiee >= start_dt)
        if end:
            end_dt = datetime.strptime(end[:10], '%Y-%m-%d')
            q = q.filter(Intervention.date_planifiee <= end_dt)
    except Exception:
        pass  # Si parsing échoue, on retourne toutes les interventions
    events = []
    for i in q.all():
        try:
            duree = int(i.duree_estimee) if i.duree_estimee else 60
            fin = i.date_planifiee + timedelta(minutes=duree)
            events.append({
                'id': i.id,
                'title': f"[{i.client.nom_affichage}] {i.titre}",
                'start': i.date_planifiee.isoformat(),
                'end': fin.isoformat(),
                'color': i.couleur,
                'url': url_for('intervention_detail', id=i.id),
                'extendedProps': {
                    'statut': i.statut_label,
                    'priorite': i.priorite,
                    'client': i.client.nom_affichage,
                    'technicien': i.technicien or '',
                },
            })
        except Exception:
            pass  # ignorer les interventions corrompues
    return jsonify(events)

@app.route('/interventions')
@login_required
def interventions_liste():
    statut   = request.args.get('statut', '')
    priorite = request.args.get('priorite', '')
    cid      = request.args.get('client_id', '')
    vue      = request.args.get('vue', 'liste')   # 'liste' ou 'dossiers'

    q = Intervention.query
    if statut:   q = q.filter_by(statut=statut)
    if priorite: q = q.filter_by(priorite=priorite)
    if cid:      q = q.filter_by(client_id=int(cid))

    # Tri : par nom client puis par date pour la vue dossiers
    interventions = (q.join(Client)
                      .order_by(Client.nom.asc(), Intervention.date_planifiee.desc())
                      .all())

    clients = Client.query.filter_by(actif=True).order_by(Client.nom).all()

    # Grouper par nom client (dict ordonné)
    from collections import OrderedDict
    groupes = OrderedDict()
    for i in interventions:
        nom = i.client.nom_affichage
        groupes.setdefault(nom, []).append(i)

    return render_template('interventions/index.html',
                           interventions=interventions,
                           groupes=groupes,
                           clients=clients,
                           statut=statut, priorite=priorite, cid=cid,
                           vue=vue)

@app.route('/interventions/nouvelle', methods=['GET', 'POST'])
@login_required
def intervention_nouvelle():
    if request.method == 'POST':
        try:
            dp = datetime.strptime(request.form['date_planifiee'], '%Y-%m-%dT%H:%M')
        except ValueError:
            dp = datetime.strptime(request.form['date_planifiee'], '%Y-%m-%d %H:%M')
        i = Intervention(
            reference=next_ref(Intervention, 'INT'),
            client_id=int(request.form['client_id']),
            titre=request.form['titre'].strip(),
            description=request.form.get('description','').strip(),
            type_intervention=request.form.get('type_intervention','').strip(),
            priorite=request.form.get('priorite','normale'),
            date_planifiee=dp,
            duree_estimee=int(request.form.get('duree_estimee', 60) or 60),
            technicien=request.form.get('technicien','').strip(),
            notes=request.form.get('notes','').strip(),
        )
        db.session.add(i)
        db.session.commit()
        flash(f'Intervention {i.reference} planifiée.', 'success')
        if request.form.get('creer_bon'):
            b = BonIntervention(numero=next_bon_num(), intervention_id=i.id)
            db.session.add(b)
            db.session.commit()
            return redirect(url_for('bon_detail', id=b.id))
        return redirect(url_for('intervention_detail', id=i.id))

    clients = Client.query.filter_by(actif=True).order_by(Client.nom).all()
    cid = request.args.get('client_id')
    techniciens = get_param('techniciens', '')
    types_inter = get_param('types_intervention', '')
    return render_template('interventions/create.html', intervention=None,
                           clients=clients, cid=int(cid) if cid else None,
                           techniciens=techniciens, types_inter=types_inter)

@app.route('/interventions/<int:id>')
@login_required
def intervention_detail(id):
    i = Intervention.query.get_or_404(id)
    return render_template('interventions/detail.html', intervention=i)

@app.route('/interventions/<int:id>/modifier', methods=['GET', 'POST'])
@login_required
def intervention_modifier(id):
    i = Intervention.query.get_or_404(id)
    if request.method == 'POST':
        try:
            i.date_planifiee = datetime.strptime(request.form['date_planifiee'], '%Y-%m-%dT%H:%M')
        except ValueError:
            i.date_planifiee = datetime.strptime(request.form['date_planifiee'], '%Y-%m-%d %H:%M')
        i.client_id = int(request.form['client_id'])
        i.titre = request.form['titre'].strip()
        i.description = request.form.get('description','').strip()
        i.type_intervention = request.form.get('type_intervention','').strip()
        i.priorite = request.form.get('priorite','normale')
        i.statut = request.form.get('statut','planifiee')
        i.duree_estimee = int(request.form.get('duree_estimee', 60) or 60)
        i.technicien = request.form.get('technicien','').strip()
        i.notes = request.form.get('notes','').strip()
        db.session.commit()
        flash('Intervention mise à jour.', 'success')
        return redirect(url_for('intervention_detail', id=id))
    clients = Client.query.filter_by(actif=True).order_by(Client.nom).all()
    techniciens = get_param('techniciens', '')
    types_inter = get_param('types_intervention', '')
    return render_template('interventions/create.html', intervention=i,
                           clients=clients, cid=i.client_id,
                           techniciens=techniciens, types_inter=types_inter)

@app.route('/interventions/<int:id>/statut', methods=['POST'])
@login_required
def intervention_statut(id):
    i = Intervention.query.get_or_404(id)
    s = request.form.get('statut')
    if s in ['planifiee','en_cours','terminee','annulee']:
        i.statut = s
        db.session.commit()
        flash(f'Statut → {i.statut_label}', 'success')
    return redirect(url_for('intervention_detail', id=id))

@app.route('/interventions/<int:id>/supprimer', methods=['POST'])
@login_required
def intervention_supprimer(id):
    i = Intervention.query.get_or_404(id)
    db.session.delete(i)
    db.session.commit()
    flash('Intervention supprimée.', 'info')
    return redirect(url_for('interventions_liste'))

# ─── BONS D'INTERVENTION ──────────────────────────────────────────────────────

@app.route('/bons')
@login_required
def bons_liste():
    statut = request.args.get('statut','')
    q = BonIntervention.query
    if statut: q = q.filter_by(statut=statut)
    bons = q.order_by(BonIntervention.date_creation.desc()).all()
    return render_template('bons/index.html', bons=bons, statut=statut)

@app.route('/bons/nouveau', methods=['GET', 'POST'])
@login_required
def bon_nouveau():
    if request.method == 'POST':
        iid = int(request.form['intervention_id'])
        inter = Intervention.query.get_or_404(iid)
        if inter.bon:
            flash('Un bon existe déjà pour cette intervention.', 'warning')
            return redirect(url_for('bon_detail', id=inter.bon.id))
        mats = _extract_mats(request)
        b = BonIntervention(
            numero=next_bon_num(), intervention_id=iid,
            travaux_effectues=request.form.get('travaux_effectues','').strip(),
            observations=request.form.get('observations','').strip(),
            recommandations=request.form.get('recommandations','').strip(),
            temps_passe=int(request.form['temps_passe']) if request.form.get('temps_passe') else None,
            materiaux_utilises=json.dumps(mats, ensure_ascii=False) if mats else None,
        )
        db.session.add(b)
        inter.statut = 'terminee'
        db.session.commit()
        flash(f'Bon N° {b.numero} créé.', 'success')
        return redirect(url_for('bon_detail', id=b.id))

    iid = request.args.get('intervention_id')
    existing_ids = db.session.query(BonIntervention.intervention_id).all()
    existing_ids = [e[0] for e in existing_ids]
    interventions = Intervention.query.filter(
        Intervention.id.notin_(existing_ids)
    ).order_by(Intervention.date_planifiee.desc()).all()
    return render_template('bons/create.html', bon=None, interventions=interventions,
                           iid=int(iid) if iid else None, materiaux=[])

@app.route('/bons/<int:id>')
@login_required
def bon_detail(id):
    b = BonIntervention.query.get_or_404(id)
    mats = json.loads(b.materiaux_utilises) if b.materiaux_utilises else []
    return render_template('bons/detail.html', bon=b, materiaux=mats)

@app.route('/bons/<int:id>/modifier', methods=['GET', 'POST'])
@login_required
def bon_modifier(id):
    b = BonIntervention.query.get_or_404(id)
    if request.method == 'POST':
        b.travaux_effectues = request.form.get('travaux_effectues','').strip()
        b.observations = request.form.get('observations','').strip()
        b.recommandations = request.form.get('recommandations','').strip()
        b.temps_passe = int(request.form['temps_passe']) if request.form.get('temps_passe') else None
        mats = _extract_mats(request)
        b.materiaux_utilises = json.dumps(mats, ensure_ascii=False) if mats else None
        if request.form.get('finaliser'):
            b.statut = 'finalise'
            b.date_finalisation = datetime.utcnow()
        db.session.commit()
        flash('Bon mis à jour.', 'success')
        return redirect(url_for('bon_detail', id=id))
    mats = json.loads(b.materiaux_utilises) if b.materiaux_utilises else []
    return render_template('bons/create.html', bon=b, intervention=b.intervention,
                           interventions=[], iid=b.intervention_id, materiaux=mats)

def _extract_mats(req):
    desig = req.form.getlist('mat_designation[]')
    qty = req.form.getlist('mat_quantite[]')
    unit = req.form.getlist('mat_unite[]')
    return [{'designation': d, 'quantite': q, 'unite': u}
            for d, q, u in zip(desig, qty, unit) if d.strip()]

@app.route('/bons/<int:id>/pdf')
@login_required
def bon_pdf(id):
    b = BonIntervention.query.get_or_404(id)
    try:
        buf = generer_pdf(b)
        return send_file(buf, download_name=f"bon_{b.numero}.pdf",
                         mimetype='application/pdf',
                         as_attachment=request.args.get('dl') == '1')
    except Exception as e:
        flash(f'Erreur PDF : {e}', 'danger')
        return redirect(url_for('bon_detail', id=id))

@app.route('/bons/<int:id>/envoyer', methods=['POST'])
@login_required
def bon_envoyer(id):
    b = BonIntervention.query.get_or_404(id)
    ok, msg = envoyer_bon_email(b)
    if ok:
        b.statut = 'envoye'
        b.date_envoi = datetime.utcnow()
        db.session.commit()
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('bon_detail', id=id))

@app.route('/test-email')
def test_email():
    env_vars = {k: ('***' if 'KEY' in k or 'PASSWORD' in k else v) for k, v in os.environ.items() if k.startswith('APP_')}
    raw_key = os.environ.get('APP_BREVO_API_KEY', '')
    api_key = ''.join(raw_key.split())
    version = 'v5-join-split'
    if not api_key:
        return jsonify({
            'statut': 'erreur',
            'message': 'APP_BREVO_API_KEY manquant — ajoutez cette variable dans Render > Environment',
            'env_vars_detectes': env_vars,
            'version_code': version,
        })
    req = urllib.request.Request(
        'https://api.brevo.com/v3/account',
        headers={'api-key': api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return jsonify({
                'statut': 'ok',
                'message': f"Connexion Brevo réussie — compte : {data.get('email', '?')}",
                'env_vars_detectes': env_vars,
                'version_code': version,
            })
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='ignore')[:300]
        return jsonify({'statut': 'erreur', 'message': f'Erreur Brevo {e.code} : {detail}', 'env_vars_detectes': env_vars, 'version_code': version, 'key_longueur': len(api_key)})
    except Exception as e:
        return jsonify({'statut': 'erreur', 'message': str(e), 'env_vars_detectes': env_vars, 'version_code': version, 'key_longueur_raw': len(raw_key), 'key_longueur_clean': len(api_key)})

@app.route('/bons/<int:id>/supprimer', methods=['POST'])
@login_required
def bon_supprimer(id):
    b = BonIntervention.query.get_or_404(id)
    iid = b.intervention_id
    db.session.delete(b)
    db.session.commit()
    flash('Bon supprimé.', 'info')
    return redirect(url_for('intervention_detail', id=iid))

# ─── TOURNÉE ──────────────────────────────────────────────────────────────────

@app.route('/tournee')
@login_required
def tournee():
    date_str = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    try:
        jour = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        jour = datetime.now().date()
    interventions = Intervention.query.filter(
        db.func.date(Intervention.date_planifiee) == jour,
        Intervention.statut.in_(['planifiee', 'en_cours'])
    ).order_by(Intervention.date_planifiee).all()
    adresse_depart = get_param('adresse', '')
    return render_template('tournee.html', interventions=interventions,
                           date_str=date_str, jour=jour,
                           adresse_depart=adresse_depart)

@app.route('/tournee/api/optimiser')
@login_required
def tournee_optimiser():
    date_str = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    depart = request.args.get('depart', '').strip()
    try:
        jour = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'Date invalide'}), 400

    interventions = Intervention.query.filter(
        db.func.date(Intervention.date_planifiee) == jour,
        Intervention.statut.in_(['planifiee', 'en_cours'])
    ).all()

    points = []
    sans_adresse = []
    for inter in interventions:
        cli = inter.client
        if not cli.adresse and not cli.ville:
            sans_adresse.append(inter.id)
            continue
        _geocoder_client(cli)
        time.sleep(0.3)  # respecter la limite Nominatim
        if cli.latitude and cli.longitude:
            points.append({
                'id': inter.id,
                'client': cli.nom_affichage,
                'adresse': f"{cli.adresse or ''} {cli.code_postal or ''} {cli.ville or ''}".strip(),
                'type': inter.type_intervention or '',
                'heure': inter.date_planifiee.strftime('%H:%M'),
                'telephone': cli.telephone or '',
                'lat': cli.latitude,
                'lon': cli.longitude,
                'url': url_for('intervention_detail', id=inter.id),
            })
        else:
            sans_adresse.append(inter.id)

    # Géocoder le point de départ si fourni
    depart_lat = depart_lon = None
    if depart:
        try:
            req = urllib.request.Request(
                f"https://nominatim.openstreetmap.org/search?q={urllib.request.quote(depart + ' France')}&format=json&limit=1",
                headers={'User-Agent': 'AgendaHPS/1.0'}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                if data:
                    depart_lat = float(data[0]['lat'])
                    depart_lon = float(data[0]['lon'])
        except Exception:
            pass

    route_optimisee = _optimiser_tournee(points, depart_lat, depart_lon)

    # Distance totale estimée
    dist_total = 0
    all_pts = ([{'lat': depart_lat, 'lon': depart_lon}] if depart_lat else []) + route_optimisee
    for i in range(len(all_pts) - 1):
        dist_total += _haversine(all_pts[i]['lat'], all_pts[i]['lon'],
                                 all_pts[i+1]['lat'], all_pts[i+1]['lon'])

    # Lien Google Maps avec tous les waypoints
    if route_optimisee:
        waypoints = '/'.join(f"{p['lat']},{p['lon']}" for p in route_optimisee)
        gmaps_url = f"https://www.google.com/maps/dir/{waypoints}"
        waze_first = f"https://waze.com/ul?ll={route_optimisee[0]['lat']},{route_optimisee[0]['lon']}&navigate=yes"
    else:
        gmaps_url = waze_first = ''

    return jsonify({
        'route': route_optimisee,
        'sans_adresse': sans_adresse,
        'distance_km': round(dist_total, 1),
        'gmaps_url': gmaps_url,
        'waze_url': waze_first,
        'depart_lat': depart_lat,
        'depart_lon': depart_lon,
    })

# ─── CONTRATS CLIENTS ────────────────────────────────────────────────────────

TYPES_PRESTATION = [
    'Dératisation',
    'Désinsectisation',
    'Désinfection',
    'Dératisation + Désinsectisation',
    'Dératisation + Désinsectisation + Désinfection',
]

def _planifier_passages_auto(client, type_prestation, passages_annuels, date_debut, date_fin):
    """Crée automatiquement les interventions planifiées pour un contrat.
    Ne crée que les passages futurs et évite les doublons (fenêtre ±15 jours)."""
    from datetime import date as date_type, timedelta
    if passages_annuels <= 0:
        return 0
    today = datetime.now().date()
    debut = date_debut or today
    fin = date_fin or date_type(debut.year, 12, 31)
    total_days = (fin - debut).days
    if total_days <= 0:
        return 0
    interval_days = total_days // passages_annuels
    created = 0
    for i in range(passages_annuels):
        planned_date = debut + timedelta(days=i * interval_days)
        if planned_date < today:
            continue  # ne pas créer dans le passé
        # Vérifier doublon ±15 jours
        win_start = datetime.combine(planned_date - timedelta(days=15), datetime.min.time())
        win_end   = datetime.combine(planned_date + timedelta(days=15), datetime.max.time())
        existing = Intervention.query.filter(
            Intervention.client_id == client.id,
            Intervention.type_intervention.ilike(f'%{type_prestation}%'),
            Intervention.date_planifiee.between(win_start, win_end),
            Intervention.statut.in_(['planifiee', 'en_cours', 'terminee'])
        ).first()
        if not existing:
            heure = datetime.combine(planned_date, datetime.strptime('08:00', '%H:%M').time())
            inter = Intervention(
                reference=next_ref(Intervention, 'INT'),
                client_id=client.id,
                titre=f'{type_prestation} — {client.nom_affichage}',
                type_intervention=type_prestation,
                priorite='normale',
                statut='planifiee',
                date_planifiee=heure,
                duree_estimee=60,
            )
            db.session.add(inter)
            created += 1
    return created


@app.route('/clients/<int:id>/contrats/sauvegarder', methods=['POST'])
@login_required
def contrats_sauvegarder(id):
    """Sauvegarde tous les types de prestation et planifie automatiquement."""
    client = Client.query.get_or_404(id)
    try:
        dd = datetime.strptime(request.form['date_debut'], '%Y-%m-%d').date() if request.form.get('date_debut') else None
        df = datetime.strptime(request.form['date_fin'], '%Y-%m-%d').date() if request.form.get('date_fin') else None
    except ValueError:
        dd = df = None
    nb = int(request.form.get('nb_types', 0))
    planifier = request.form.get('planifier_auto') == '1'
    total_crees = 0
    for i in range(1, nb + 1):
        type_p = request.form.get(f'type_{i}', '').strip()
        passages = int(request.form.get(f'passages_{i}') or 0)
        if not type_p:
            continue
        existing = ContratClient.query.filter_by(client_id=id, type_prestation=type_p).first()
        if existing:
            existing.passages_annuels = passages
            existing.date_debut = dd
            existing.date_fin = df
        else:
            db.session.add(ContratClient(
                client_id=id, type_prestation=type_p,
                passages_annuels=passages, date_debut=dd, date_fin=df
            ))
        # Planification automatique si demandé et passages > 0
        if planifier and passages > 0:
            db.session.flush()  # pour avoir les IDs
            total_crees += _planifier_passages_auto(client, type_p, passages, dd, df)
    db.session.commit()
    if planifier and total_crees > 0:
        flash(f'Contrats mis à jour + {total_crees} intervention(s) planifiée(s) automatiquement dans l\'agenda.', 'success')
    else:
        flash('Contrats mis à jour.', 'success')
    return redirect(url_for('client_detail', id=id))

@app.route('/contrats/<int:cid>/supprimer', methods=['POST'])
@login_required
def contrat_supprimer(cid):
    c = ContratClient.query.get_or_404(cid)
    client_id = c.client_id
    db.session.delete(c)
    db.session.commit()
    flash('Contrat supprimé.', 'info')
    return redirect(url_for('client_detail', id=client_id))

# ─── PHOTOS BONS ─────────────────────────────────────────────────────────────

@app.route('/bons/<int:id>/photos/ajouter', methods=['POST'])
@login_required
def bon_photo_ajouter(id):
    b = BonIntervention.query.get_or_404(id)
    file = request.files.get('photo')
    if not file or not file.filename:
        flash('Aucun fichier sélectionné.', 'warning')
        return redirect(url_for('bon_detail', id=id))
    # Vérifie que c'est bien une image
    if not file.mimetype.startswith('image/'):
        flash('Le fichier doit être une image.', 'danger')
        return redirect(url_for('bon_detail', id=id))
    # Limite à 8 Mo
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > 8 * 1024 * 1024:
        flash('Image trop lourde (max 8 Mo). Compressez-la avant.', 'danger')
        return redirect(url_for('bon_detail', id=id))
    data_b64 = base64.b64encode(file.read()).decode('utf-8')
    photo = BonPhoto(bon_id=id, nom=file.filename,
                     data=data_b64, mimetype=file.mimetype)
    db.session.add(photo)
    db.session.commit()
    flash('Photo ajoutée.', 'success')
    return redirect(url_for('bon_detail', id=id))

@app.route('/bons/photos/<int:photo_id>')
@login_required
def bon_photo_voir(photo_id):
    p = BonPhoto.query.get_or_404(photo_id)
    img_bytes = base64.b64decode(p.data)
    return send_file(io.BytesIO(img_bytes), mimetype=p.mimetype,
                     download_name=p.nom)

@app.route('/bons/photos/<int:photo_id>/supprimer', methods=['POST'])
@login_required
def bon_photo_supprimer(photo_id):
    p = BonPhoto.query.get_or_404(photo_id)
    bid = p.bon_id
    db.session.delete(p)
    db.session.commit()
    flash('Photo supprimée.', 'info')
    return redirect(url_for('bon_detail', id=bid))

# ─── PORTAIL CLIENT ───────────────────────────────────────────────────────────

@app.route('/portail/<token>', methods=['GET', 'POST'])
def portail_access(token):
    c = Client.query.filter_by(access_token=token, portal_actif=True).first()
    if not c: abort(404)
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'register' and not c.portal_password_hash:
            pwd = request.form.get('password','')
            cpwd = request.form.get('confirm_password','')
            if len(pwd) < 6:
                flash('Mot de passe trop court (6 caractères minimum).', 'danger')
            elif pwd != cpwd:
                flash('Les mots de passe ne correspondent pas.', 'danger')
            else:
                c.set_portal_password(pwd)
                db.session.commit()
                flask_session['portal_cid'] = c.id
                flash('Compte créé ! Bienvenue.', 'success')
                return redirect(url_for('portail_dashboard', token=token))
        elif action == 'login':
            if c.check_portal_password(request.form.get('password','')):
                flask_session['portal_cid'] = c.id
                flash(f'Bienvenue, {c.prenom or c.nom} !', 'success')
                return redirect(url_for('portail_dashboard', token=token))
            flash('Mot de passe incorrect.', 'danger')
    return render_template('portal/access.html', client=c, token=token,
                           is_registered=bool(c.portal_password_hash))

@app.route('/portail/<token>/dashboard')
def portail_dashboard(token):
    c = Client.query.filter_by(access_token=token, portal_actif=True).first_or_404()
    if flask_session.get('portal_cid') != c.id:
        return redirect(url_for('portail_access', token=token))
    now = datetime.now()
    a_venir = Intervention.query.filter_by(client_id=c.id)\
        .filter(Intervention.date_planifiee >= now,
                Intervention.statut.in_(['planifiee', 'en_cours']))\
        .order_by(Intervention.date_planifiee.asc()).all()
    passes = Intervention.query.filter_by(client_id=c.id)\
        .filter(db.or_(Intervention.date_planifiee < now,
                       Intervention.statut.in_(['terminee', 'annulee'])))\
        .order_by(Intervention.date_planifiee.desc()).all()
    bons = BonIntervention.query.join(Intervention)\
        .filter(Intervention.client_id == c.id,
                BonIntervention.statut.in_(['finalise', 'envoye', 'signe']))\
        .order_by(BonIntervention.date_creation.desc()).all()
    soc = get_param('societe', 'HPS')
    tel_soc = get_param('telephone', '')
    email_soc = get_param('email', '')
    return render_template('portal/dashboard.html', client=c,
                           a_venir=a_venir, passes=passes, bons=bons,
                           token=token, soc=soc, tel_soc=tel_soc,
                           email_soc=email_soc)

@app.route('/portail/<token>/bon/<int:bid>/pdf')
def portail_bon_pdf(token, bid):
    c = Client.query.filter_by(access_token=token, portal_actif=True).first_or_404()
    if flask_session.get('portal_cid') != c.id:
        return redirect(url_for('portail_access', token=token))
    b = BonIntervention.query.get_or_404(bid)
    if b.intervention.client_id != c.id: abort(403)
    buf = generer_pdf(b)
    return send_file(buf, download_name=f"bon_{b.numero}.pdf", mimetype='application/pdf')

@app.route('/portail/<token>/bon/<int:bid>/signer', methods=['POST'])
def portail_signer(token, bid):
    c = Client.query.filter_by(access_token=token, portal_actif=True).first_or_404()
    if flask_session.get('portal_cid') != c.id:
        return redirect(url_for('portail_access', token=token))
    b = BonIntervention.query.get_or_404(bid)
    if b.intervention.client_id != c.id: abort(403)
    b.signature_client = True
    b.date_signature = datetime.utcnow()
    b.statut = 'signe'
    db.session.commit()
    flash('Bon signé électroniquement. Merci !', 'success')
    return redirect(url_for('portail_dashboard', token=token))

@app.route('/portail/<token>/deconnexion')
def portail_deconnexion(token):
    flask_session.pop('portal_cid', None)
    return redirect(url_for('portail_access', token=token))

# ─── PARAMÈTRES ───────────────────────────────────────────────────────────────

@app.route('/parametres', methods=['GET', 'POST'])
@login_required
def parametres():
    if request.method == 'POST':
        for k in ['societe','adresse','telephone','email','siret',
                  'mail_server','mail_port','mail_username','mail_password',
                  'mail_use_tls','base_url','techniciens','types_intervention']:
            if k in request.form:
                set_param(k, request.form[k])
        if request.form.get('nouveau_mdp'):
            if request.form['nouveau_mdp'] == request.form.get('confirm_mdp',''):
                current_user.set_password(request.form['nouveau_mdp'])
                db.session.commit()
                flash('Mot de passe mis à jour.', 'success')
            else:
                flash('Les mots de passe ne correspondent pas.', 'danger')
        flash('Paramètres enregistrés.', 'success')
        return redirect(url_for('parametres'))
    params = {p.cle: p.valeur for p in Parametre.query.all()}
    return render_template('parametres.html', params=params)

# ─── DIAGNOSTIC ──────────────────────────────────────────────────────────────

@app.route('/healthz')
def healthz():
    import traceback
    try:
        User.query.count()
        db_ok = True
        db_err = None
    except Exception as e:
        db_ok = False
        db_err = traceback.format_exc()
    return jsonify({
        'status': 'ok' if db_ok else 'error',
        'db': str(app.config['SQLALCHEMY_DATABASE_URI'])[:40] + '...',
        'db_ok': db_ok,
        'db_error': db_err,
        'python': __import__('sys').version,
    })

# ─── INIT ─────────────────────────────────────────────────────────────────────

def init_db():
    with app.app_context():
        db.create_all()
        # ── Migrations manuelles : ajout de colonnes sur tables existantes ──
        migrations = [
            "ALTER TABLE clients ADD COLUMN IF NOT EXISTS latitude FLOAT",
            "ALTER TABLE clients ADD COLUMN IF NOT EXISTS longitude FLOAT",
        ]
        for sql in migrations:
            try:
                db.session.execute(db.text(sql))
                db.session.commit()
            except Exception:
                db.session.rollback()
        # ── Compte admin par défaut ──────────────────────────────────────────
        if not User.query.filter_by(username='admin').first():
            a = User(username='admin', nom='Administrateur', is_admin=True)
            a.set_password('Admin123!')
            db.session.add(a)
            db.session.commit()
            print("=" * 55)
            print("  Compte admin cree  :  admin / Admin123!")
            print("  !! Changez ce mot de passe dans Parametres !!")
            print("=" * 55)

# Initialisation automatique au démarrage (local ET production Gunicorn)
init_db()

# Auto-ping toutes les 10 min pour éviter la mise en veille Render (plan gratuit)
def _keep_alive():
    url = os.environ.get('BASE_URL', '').strip()
    if not url or 'localhost' in url:
        return  # Désactivé en local
    while True:
        time.sleep(240)  # 4 minutes (Render dors après 15 min sans requête)
        try:
            urllib.request.urlopen(url + '/healthz', timeout=10)
        except Exception:
            pass

_t = threading.Thread(target=_keep_alive, daemon=True)
_t.start()

if __name__ == '__main__':
    init_db()
    print("\n  Agenda & Bons d'Intervention")
    print("  http://localhost:5000")
    print()
    app.run(debug=False, host='0.0.0.0', port=5000)
