import os, uuid, io, json, threading, time, base64, urllib.request, urllib.error
from collections import OrderedDict
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
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer, Image as RLImage)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    PDF_OK = True
except ImportError:
    PDF_OK = False

app = Flask(__name__)
# Render / Gunicorn tournent derrière un reverse-proxy HTTPS.
# ProxyFix lit les headers X-Forwarded-Proto/Host pour que Flask
# génère correctement des URLs en https:// au lieu de http://
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

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
    notifs_actives = db.Column(db.Boolean, default=True)  # alertes email portail
    sms_actif = db.Column(db.Boolean, default=False)       # alertes SMS portail
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
        t = (self.type_intervention or '').lower()
        if 'dératisation' in t:    return '#e67e22'   # orange
        if 'désinsectisation' in t: return '#1aabe3'  # bleu HPS
        if 'désinfection' in t:    return '#27ae60'   # vert
        if 'dépigeonnage' in t:    return '#9b59b6'   # violet
        if 'taupe' in t:           return '#795548'   # marron
        if 'frelon' in t:          return '#e74c3c'   # rouge
        if 'guêpe' in t or 'guepe' in t: return '#f39c12'  # jaune-orange
        if 'abeille' in t:         return '#f1c40f'   # jaune
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
    signature_image = db.Column(db.Text)        # base64 PNG de la signature
    signature_technicien = db.Column(db.Text)   # base64 PNG signature technicien

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


class PlanAppatage(db.Model):
    __tablename__ = 'plans_appatage'
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    nom = db.Column(db.String(200), default='Plan')   # nom/étiquette
    notes = db.Column(db.Text)                         # remarques libres
    data = db.Column(db.Text, nullable=False)          # contenu encodé en base64
    mimetype = db.Column(db.String(80), default='image/jpeg')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    client = db.relationship('Client', backref='plans_appatage')


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

# Noms français des jours et mois (indépendant de la locale système)
_JOURS_FR  = ['Lundi','Mardi','Mercredi','Jeudi','Vendredi','Samedi','Dimanche']
_MOIS_FR   = ['janvier','février','mars','avril','mai','juin',
               'juillet','août','septembre','octobre','novembre','décembre']

@app.template_filter('date_fr')
def date_fr_filter(dt, fmt='%d/%m/%Y'):
    """Formate une date en remplaçant %A/%a (jour) et %B/%b (mois) par leur équivalent français."""
    if not dt:
        return ''
    fmt = fmt.replace('%A', _JOURS_FR[dt.weekday()])
    fmt = fmt.replace('%a', _JOURS_FR[dt.weekday()][:3])
    fmt = fmt.replace('%B', _MOIS_FR[dt.month - 1])
    fmt = fmt.replace('%b', _MOIS_FR[dt.month - 1][:3])
    return dt.strftime(fmt)

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

    # ── En-tête : logo HPS à gauche, infos société à droite ──
    static_dir = os.path.join(os.path.dirname(__file__), 'static')
    logo_hps_path = os.path.join(static_dir, 'logo.png')

    s_soc_name = ParagraphStyle('sn', parent=styles['Normal'], fontSize=13,
                                fontName='Helvetica-Bold', alignment=TA_RIGHT)
    s_soc_info = ParagraphStyle('si', parent=styles['Normal'], fontSize=8,
                                alignment=TA_RIGHT, spaceAfter=2)

    info_soc = '\n'.join(filter(None, [
        get_param('adresse'),
        get_param('telephone'),
        get_param('email'),
        f"SIRET : {get_param('siret')}" if get_param('siret') else None,
    ]))

    if os.path.exists(logo_hps_path):
        try:
            logo_hps = RLImage(logo_hps_path, width=4.5*cm, height=2*cm, kind='proportional')
            header_data = [[logo_hps, Paragraph(f"<b>{soc}</b><br/><font size='8'>{info_soc.replace(chr(10), '<br/>')}</font>",
                                                ParagraphStyle('sh', parent=styles['Normal'], fontSize=9, alignment=TA_RIGHT))]]
            header_table = Table(header_data, colWidths=[6*cm, 11.5*cm])
            header_table.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('LINEBELOW', (0, 0), (-1, 0), 1, colors.HexColor('#1aabe3')),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ]))
            elems.append(header_table)
        except Exception:
            elems.append(Paragraph(soc, s_titre))
            if info_soc:
                elems.append(Paragraph(info_soc.replace('\n', ' | '), ParagraphStyle(
                    'si_fb', parent=styles['Normal'], fontSize=8, alignment=TA_CENTER, spaceAfter=4)))
    else:
        elems.append(Paragraph(soc, s_titre))
        if info_soc:
            elems.append(Paragraph(info_soc.replace('\n', ' | '), ParagraphStyle(
                'si_fb2', parent=styles['Normal'], fontSize=8, alignment=TA_CENTER, spaceAfter=4)))

    elems.append(Spacer(1, 0.4*cm))
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

    # ── Pied de page : logos partenaires Prosane + CEPA ──
    logo_prosane_path = os.path.join(static_dir, 'logo_prosane.png')
    logo_cepa_path    = os.path.join(static_dir, 'logo_cepa.png')
    # Essayer aussi les extensions .jpg
    if not os.path.exists(logo_prosane_path):
        logo_prosane_path = os.path.join(static_dir, 'logo_prosane.jpg')
    if not os.path.exists(logo_cepa_path):
        logo_cepa_path = os.path.join(static_dir, 'logo_cepa.jpg')

    has_prosane = os.path.exists(logo_prosane_path)
    has_cepa    = os.path.exists(logo_cepa_path)

    if has_prosane or has_cepa:
        elems.append(Spacer(1, 0.5*cm))
        # Ligne séparatrice bleue
        sep = Table([['']], colWidths=[17.5*cm])
        sep.setStyle(TableStyle([('LINEABOVE', (0,0), (-1,0), 0.8, colors.HexColor('#1aabe3'))]))
        elems.append(sep)
        elems.append(Spacer(1, 0.3*cm))

        # Texte centré
        elems.append(Paragraph("Produits homologués et certifications :",
                               ParagraphStyle('lp', parent=styles['Normal'], fontSize=7,
                                              textColor=colors.grey, alignment=TA_CENTER, spaceAfter=6)))

        # Logos centrés
        logo_cells = []
        if has_prosane and has_cepa:
            try:
                img_p = RLImage(logo_prosane_path, width=3.5*cm, height=1.5*cm, kind='proportional')
                img_c = RLImage(logo_cepa_path,    width=3.5*cm, height=1.5*cm, kind='proportional')
                logo_cells = [['', img_p, '', img_c, '']]
                col_w = [3.375*cm, 3.5*cm, 3.75*cm, 3.5*cm, 3.375*cm]
            except Exception:
                logo_cells = [['', 'Prosane', '', 'CEPA', '']]
                col_w = [3.375*cm, 3.5*cm, 3.75*cm, 3.5*cm, 3.375*cm]
        elif has_prosane:
            try:
                img_p = RLImage(logo_prosane_path, width=3.5*cm, height=1.5*cm, kind='proportional')
                logo_cells = [['', img_p, '']]
                col_w = [7*cm, 3.5*cm, 7*cm]
            except Exception:
                logo_cells = [['', 'Prosane', '']]
                col_w = [7*cm, 3.5*cm, 7*cm]
        else:
            try:
                img_c = RLImage(logo_cepa_path, width=3.5*cm, height=1.5*cm, kind='proportional')
                logo_cells = [['', img_c, '']]
                col_w = [7*cm, 3.5*cm, 7*cm]
            except Exception:
                logo_cells = [['', 'CEPA', '']]
                col_w = [7*cm, 3.5*cm, 7*cm]

        ft = Table(logo_cells, colWidths=col_w)
        ft.setStyle(TableStyle([
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('ALIGN',  (0,0), (-1,-1), 'CENTER'),
        ]))
        elems.append(ft)

    doc.build(elems)
    buf.seek(0)
    return buf


def formater_telephone_international(tel):
    """Convertit un numéro français en format international E.164 (+33...)."""
    if not tel:
        return None
    # Nettoyage : supprimer espaces, tirets, points, parenthèses
    t = ''.join(c for c in tel if c.isdigit() or c == '+')
    if t.startswith('+33'):
        pass  # déjà au bon format
    elif t.startswith('33') and len(t) >= 11:
        t = '+' + t
    elif t.startswith('0') and len(t) == 10:
        t = '+33' + t[1:]
    else:
        return None  # format non reconnu
    # Vérification : doit faire 12 caractères (+33XXXXXXXXX)
    if len(t) != 12:
        return None
    return t


def envoyer_sms_client(client, message):
    """Envoie un SMS au client via l'API SMS Brevo (même clé que les emails)."""
    api_key = ''.join(os.environ.get('APP_BREVO_API_KEY', '').split())
    if not api_key:
        return False, "Clé API Brevo manquante."
    tel = formater_telephone_international(client.telephone)
    if not tel:
        # Essayer le second numéro
        tel = formater_telephone_international(client.telephone2) if client.telephone2 else None
    if not tel:
        return False, "Numéro de téléphone invalide ou manquant."
    soc = get_param('societe', 'HPS')
    # Sender : max 11 caractères alphanumériques, pas d'espaces
    sender = ''.join(c for c in soc if c.isalnum())[:11] or 'HPS'
    # Message : max 160 caractères pour un SMS simple
    if len(message) > 160:
        message = message[:157] + '...'
    payload = json.dumps({
        "sender": sender,
        "recipient": tel,
        "content": message,
        "type": "transactional"
    }).encode('utf-8')
    req = urllib.request.Request(
        'https://api.brevo.com/v3/transactionalSMS/sms',
        data=payload,
        headers={'api-key': api_key, 'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status in (200, 201, 202):
                return True, f"SMS envoyé à {tel}."
            return False, f"Erreur Brevo SMS : statut {resp.status}"
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        return False, f"Erreur Brevo SMS {e.code} : {body}"
    except Exception as e:
        return False, str(e)


def envoyer_notif_client(client, sujet, titre_notif, corps_texte, lien_portail=None):
    """Envoie une notification email au client via Brevo (dans un thread séparé)."""
    api_key = ''.join(os.environ.get('APP_BREVO_API_KEY', '').split())
    if not api_key:
        return False, "Clé API Brevo manquante."
    if not client.email:
        return False, "Pas d'email pour ce client."
    if client.notifs_actives is False:
        return False, "Notifications désactivées pour ce client."
    soc = get_param('societe', 'HPS')
    tel_soc = get_param('telephone', '')
    sender_email = get_param('mail_username') or get_param('email')
    if not sender_email:
        return False, "Email expéditeur manquant dans les Paramètres."
    # Bouton portail
    btn_html = ''
    if lien_portail:
        btn_html = f'''<div style="text-align:center;margin:24px 0">
          <a href="{lien_portail}" style="background:#1aabe3;color:#fff;padding:12px 28px;
             border-radius:6px;text-decoration:none;font-weight:bold;font-size:15px">
            Accéder à mon espace client →
          </a></div>'''
    # Corps HTML
    html = f"""<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto;padding:20px;color:#333">
  <div style="background:#1aabe3;padding:16px 24px;border-radius:8px 8px 0 0">
    <h2 style="color:#fff;margin:0;font-size:18px">{soc}</h2>
  </div>
  <div style="border:1px solid #e0e0e0;border-top:none;padding:24px;border-radius:0 0 8px 8px">
    <p>Bonjour <strong>{client.prenom or client.nom}</strong>,</p>
    <div style="background:#f8f9fa;border-left:4px solid #1aabe3;padding:12px 16px;border-radius:4px;margin:16px 0">
      {corps_texte.replace(chr(10), '<br>')}
    </div>
    {btn_html}
    <hr style="border:none;border-top:1px solid #eee;margin:20px 0">
    <p style="color:#888;font-size:13px;margin:0">
      {soc}{(' — ' + tel_soc) if tel_soc else ''}<br>
      <small>Vous recevez cet email car vous avez un espace client actif.
      Pour désactiver ces notifications, contactez-nous.</small>
    </p>
  </div>
</body></html>"""
    payload = json.dumps({
        "sender": {"name": soc, "email": sender_email},
        "to": [{"email": client.email, "name": client.nom_affichage}],
        "subject": f"[{soc}] {sujet}",
        "htmlContent": html,
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
                return True, "Notification envoyée."
            return False, f"Erreur Brevo : statut {resp.status}"
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        return False, f"Erreur Brevo {e.code} : {body}"
    except Exception as e:
        return False, str(e)


def _notif_async(client_id, sujet, titre, corps, lien=None, sms_court=None):
    """Lance email + SMS dans un thread pour ne pas bloquer la réponse."""
    def _run():
        try:
            with app.app_context():
                c = Client.query.get(client_id)
                if not c or not c.portal_actif:
                    return
                # Email
                if c.notifs_actives and c.email:
                    envoyer_notif_client(c, sujet, titre, corps, lien)
                # SMS
                if c.sms_actif and (c.telephone or c.telephone2):
                    msg = sms_court or f"{get_param('societe','HPS')} : {sujet}"
                    if lien:
                        msg += f" {lien}"
                    envoyer_sms_client(c, msg)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


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
    lien = url_for('portail_access', token=c.access_token, _external=True)
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
        # Notification de bienvenue
        lien = url_for('portail_access', token=c.access_token, _external=True)
        _notif_async(c.id,
            "Votre espace client est prêt !",
            "Bienvenue",
            f"Votre espace client personnel vient d'être activé.\n"
            f"Vous pouvez désormais consulter vos interventions, "
            f"télécharger vos bons et signer électroniquement depuis votre espace sécurisé.",
            lien,
            sms_court=f"Bonjour {c.prenom or c.nom}, votre espace client est pret ! Acces : {lien}")
    return redirect(url_for('client_detail', id=id))

@app.route('/clients/<int:id>/portail/desactiver', methods=['POST'])
@login_required
def portail_desactiver(id):
    c = Client.query.get_or_404(id)
    c.portal_actif = False
    db.session.commit()
    flash('Portail client désactivé.', 'info')
    return redirect(url_for('client_detail', id=id))

@app.route('/clients/<int:id>/notifs/toggle', methods=['POST'])
@login_required
def client_notifs_toggle(id):
    c = Client.query.get_or_404(id)
    c.notifs_actives = not bool(c.notifs_actives)
    db.session.commit()
    etat = "activées" if c.notifs_actives else "désactivées"
    flash(f'Notifications email {etat} pour {c.nom_affichage}.', 'success')
    return redirect(url_for('client_detail', id=id))

@app.route('/clients/<int:id>/sms/toggle', methods=['POST'])
@login_required
def client_sms_toggle(id):
    c = Client.query.get_or_404(id)
    if not c.telephone and not c.telephone2:
        flash('Ce client n\'a pas de numéro de téléphone.', 'warning')
        return redirect(url_for('client_detail', id=id))
    c.sms_actif = not bool(c.sms_actif)
    db.session.commit()
    etat = "activées" if c.sms_actif else "désactivées"
    flash(f'Alertes SMS {etat} pour {c.nom_affichage}.', 'success')
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
        # Notification au client si portail actif
        date_str = i.date_planifiee.strftime('%d/%m/%Y à %H:%M')
        lien_p = url_for('portail_access', token=i.client.access_token, _external=True) if i.client.access_token else None
        _notif_async(i.client_id,
            f"Nouveau rendez-vous planifié le {i.date_planifiee.strftime('%d/%m/%Y')}",
            "Nouveau rendez-vous",
            f"Un nouveau rendez-vous a été planifié pour vous :\n\n"
            f"📅 Date : {date_str}\n"
            f"🔧 Type : {i.type_intervention or i.titre}\n"
            + (f"👷 Technicien : {i.technicien}\n" if i.technicien else "") +
            f"\nRetrouvez tous les détails dans votre espace client.",
            lien_p,
            sms_court=f"RDV planifie le {i.date_planifiee.strftime('%d/%m/%Y')} - {i.type_intervention or i.titre}. Consultez votre espace : {lien_p or ''}")
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
        # Notification au client
        lien_p = url_for('portail_access', token=inter.client.access_token, _external=True) if inter.client.access_token else None
        _notif_async(inter.client_id,
            f"Votre bon d'intervention N° {b.numero} est disponible",
            "Bon d'intervention disponible",
            f"Votre bon d'intervention N° {b.numero} du {inter.date_planifiee.strftime('%d/%m/%Y')} "
            f"est disponible dans votre espace client.\n\n"
            f"Vous pouvez le consulter, le télécharger en PDF et le signer électroniquement.",
            lien_p,
            sms_court=f"Bon {b.numero} disponible. Consultez et signez sur votre espace : {lien_p or ''}")
        return redirect(url_for('bon_detail', id=b.id))

    iid = request.args.get('intervention_id')
    existing_ids = db.session.query(BonIntervention.intervention_id).all()
    existing_ids = [e[0] for e in existing_ids]
    interventions = Intervention.query.filter(
        Intervention.id.notin_(existing_ids)
    ).order_by(Intervention.date_planifiee.desc()).all()
    return render_template('bons/create.html', bon=None, interventions=interventions,
                           iid=int(iid) if iid else None, materiaux=[],
                           travaux_modeles=TRAVAUX_MODELES,
                           preconisations_modeles=PRECONISATIONS_MODELES)

@app.route('/bons/<int:id>')
@login_required
def bon_detail(id):
    b = BonIntervention.query.get_or_404(id)
    mats = json.loads(b.materiaux_utilises) if b.materiaux_utilises else []
    return render_template('bons/detail.html', bon=b, materiaux=mats)

@app.route('/bons/<int:id>/signer', methods=['POST'])
@login_required
def bon_signer(id):
    b = BonIntervention.query.get_or_404(id)
    sig_client = request.form.get('signature_client_data', '').strip()
    sig_tech   = request.form.get('signature_technicien_data', '').strip()
    if sig_client:
        b.signature_image  = sig_client
        b.signature_client = True
        b.date_signature   = datetime.utcnow()
        b.statut = 'signe'
    if sig_tech:
        b.signature_technicien = sig_tech
        if b.statut == 'brouillon':
            b.statut = 'finalise'
            b.date_finalisation = datetime.utcnow()
    db.session.commit()
    flash('Signature enregistrée avec succès.', 'success')
    return redirect(url_for('bon_detail', id=id))

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
            # Notification finalisation
            inter = b.intervention
            lien_p = url_for('portail_access', token=inter.client.access_token, _external=True) if inter.client.access_token else None
            _notif_async(inter.client_id,
                f"Votre compte-rendu d'intervention est prêt — N° {b.numero}",
                "Compte-rendu finalisé",
                f"Le compte-rendu de votre intervention du {inter.date_planifiee.strftime('%d/%m/%Y')} "
                f"(bon N° {b.numero}) a été finalisé par votre technicien.\n\n"
                f"Il est maintenant disponible dans votre espace client pour consultation et signature.",
                lien_p,
                sms_court=f"Compte-rendu {b.numero} finalise, pret a signer. Acces : {lien_p or ''}")

        # ── Gestion photo ajoutée depuis le formulaire modifier ──
        if request.form.get('ajouter_photo'):
            file = request.files.get('photo_upload')
            if file and file.filename and file.mimetype.startswith('image/'):
                file.seek(0, 2); size = file.tell(); file.seek(0)
                if size <= 8 * 1024 * 1024:
                    data_b64 = base64.b64encode(file.read()).decode('utf-8')
                    photo = BonPhoto(bon_id=id, nom=file.filename,
                                     data=data_b64, mimetype=file.mimetype)
                    db.session.add(photo)
                    flash('Photo ajoutée.', 'success')
                else:
                    flash('Image trop lourde (max 8 Mo).', 'danger')

        db.session.commit()
        if not request.form.get('ajouter_photo'):
            flash('Bon mis à jour.', 'success')
        return redirect(url_for('bon_modifier', id=id))
    mats = json.loads(b.materiaux_utilises) if b.materiaux_utilises else []
    return render_template('bons/create.html', bon=b, intervention=b.intervention,
                           interventions=[], iid=b.intervention_id, materiaux=mats,
                           travaux_modeles=TRAVAUX_MODELES,
                           preconisations_modeles=PRECONISATIONS_MODELES)

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
@login_required
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
    'Dépigeonnage',
    'Taupe',
    'Nid frelons',
    'Frelons asiatiques',
    'Guêpes',
    'Abeilles',
]

# Modèles de travaux effectués — groupés par type de prestation
TRAVAUX_MODELES = [
    # ══════════════════ DÉRATISATION ══════════════════
    {"groupe": "Dératisation", "label": "Pose d'appâts rodenticides",
     "texte": "Pose d'appâts rodenticides sécurisés dans les zones d'activité rongeurs (dépôts de fèces, coulées, traces). Sécurisation des postes d'appâtage. Inspection des accès et points d'entrée potentiels. Recommandations transmises au client."},
    {"groupe": "Dératisation", "label": "Relevé et renouvellement des postes",
     "texte": "Relevé et renouvellement des postes d'appâtage. Consommation constatée : légère / moyenne / forte (à compléter). Ajustement du dispositif en fonction de l'activité rongeurs. Contrôle des accès."},
    {"groupe": "Dératisation", "label": "Remplacement appâts",
     "texte": "Remplacement des appâts rodenticides consommés ou dégradés. Vérification de l'état des postes d'appâtage. Consommation notée par poste. Activité rongeurs : active / en baisse / nulle (à compléter). Prochain passage planifié."},
    {"groupe": "Dératisation", "label": "Audit infestation",
     "texte": "Audit complet des locaux : inspection visuelle de l'ensemble des zones (caves, locaux techniques, périphérie bâtiment, faux plafonds, gaines). Identification des signes d'activité rongeurs (fèces, coulées, gnures, odeurs). Cartographie des zones à risque réalisée. Plan d'action proposé au client."},
    {"groupe": "Dératisation", "label": "Contrôle mensuel",
     "texte": "Visite de contrôle mensuelle. Relevé de l'ensemble des postes d'appâtage intérieurs et extérieurs. Consommation globale : à compléter. Aucune nouvelle activité détectée / Activité résiduelle constatée (à compléter). Renouvellement des postes consommés. Compte rendu transmis."},
    {"groupe": "Dératisation", "label": "Mise en conformité HACCP",
     "texte": "Intervention dans le cadre de la mise en conformité HACCP. Mise en place ou révision du plan de lutte antiparasitaire. Positionnement des postes d'appâtage conformément au plan de maîtrise sanitaire. Cartographie remise au client. Documentation conforme aux exigences réglementaires."},
    {"groupe": "Dératisation", "label": "Contrôle extérieur",
     "texte": "Contrôle des postes d'appâtage extérieurs (périphérie bâtiment, clôtures, zones de stockage, quais). Inspection des entrées de terriers et des coulées en périphérie. Renouvellement des appâts consommés. Aucune intrusion récente constatée / Points d'entrée identifiés (à compléter). Recommandations de colmatage transmises."},
    {"groupe": "Dératisation", "label": "Contrôle faux plafonds",
     "texte": "Inspection des faux plafonds et combles à la recherche de signes d'activité rongeurs (fèces, nids, gnures, odeurs). Zones contrôlées : à compléter. Activité détectée : oui / non (à compléter). Pose ou repositionnement de postes d'appâtage si nécessaire. Accès bouchés ou signalés pour intervention maçonnerie."},
    {"groupe": "Dératisation", "label": "Inspection caméra",
     "texte": "Inspection des réseaux, gaines et zones inaccessibles par caméra endoscopique. Localisation des coulées et passages empruntés par les rongeurs. Images transmises au client. Détection de points d'entrée ou de nids. Plan d'action établi en conséquence."},
    {"groupe": "Dératisation", "label": "Dépistage positif — mise en place du plan",
     "texte": "Dépistage positif rongeurs. Mise en place du plan d'action : pose de postes d'appâtage intérieurs et extérieurs. Localisation des terriers et coulées. Conseils de prévention transmis au client (stockage, obturations)."},
    {"groupe": "Dératisation", "label": "Fin de traitement — levée de doute",
     "texte": "Relevé final des postes d'appâtage. Aucune consommation constatée. Activité rongeurs nulle. Retrait des postes ou maintien préventif selon accord client. Bilan transmis."},

    # ══════════════════ DÉSINSECTISATION ══════════════════
    {"groupe": "Désinsectisation", "label": "Traitement insecticide — pulvérisation",
     "texte": "Traitement insecticide par pulvérisation des zones infestées (plinthes, recoins, joints, passages). Produit homologué utilisé. Zone traitée laissée vide pendant 2h minimum. Aération recommandée avant retour."},
    {"groupe": "Désinsectisation", "label": "Pose de plaques engluantes",
     "texte": "Pose de pièges à insectes (plaques engluantes / phéromones) dans les zones sensibles. Inspection des zones de stockage, sanitaires et locaux techniques. Compte rendu de l'infestation transmis."},
    {"groupe": "Désinsectisation", "label": "Cafards cuisine",
     "texte": "Traitement anti-cafards ciblé en cuisine professionnelle. Application de gel insecticide aux points stratégiques (dessous équipements, joints, prises, arrière mobilier). Traitement des évacuations et locaux déchets. Localisation des foyers d'infestation. Résultat attendu sous 2 à 3 semaines. Visite de contrôle planifiée."},
    {"groupe": "Désinsectisation", "label": "Blattes germaniques",
     "texte": "Traitement anti-blattes germaniques (Blattella germanica) par application de gel appât insecticide aux points d'infestation (meubles, électroménager, gaines). Pose de pièges de monitoring. Forte résistance possible aux pyréthrinoïdes — produit rotatif utilisé. Second passage prévu à J+21."},
    {"groupe": "Désinsectisation", "label": "Punaises de lit",
     "texte": "Traitement anti-punaises de lit par application d'insecticide contact sur les zones de repos, sommiers, cadres de lit, plinthes. Mise en sac des textiles recommandée. Second traitement prévu à J+14."},
    {"groupe": "Désinsectisation", "label": "Fourmis",
     "texte": "Traitement anti-fourmis par pose d'appâts gel et pulvérisation des pistes et entrées de colonie. Traitement périphérique extérieur réalisé. Efficacité visible sous 5 à 10 jours."},
    {"groupe": "Désinsectisation", "label": "Mouches",
     "texte": "Traitement anti-mouches par pose de pièges lumineux UV et/ou application d'insecticide. Inspection des zones sources (déchets, évacuations, matières organiques). Traitement des zones de repos et d'accès. Recommandations d'hygiène transmises au client. Résultat visible sous 48h."},
    {"groupe": "Désinsectisation", "label": "Traitement vapeur",
     "texte": "Traitement thermique par vapeur sèche des zones infestées (matelas, sommiers, canapés, plinthes). Température appliquée : 120°C en surface — léthale pour tous les stades de développement. Aucun produit chimique. Zone traitée réutilisable immédiatement."},
    {"groupe": "Désinsectisation", "label": "Traitement gel insecticide",
     "texte": "Application de gel insecticide appât (attractif + insecticide) aux points d'infestation et de passage. Technique propre, sans odeur, sans évacuation des locaux. Action en réseau sur la colonie. Contrôle à J+15 recommandé."},
    {"groupe": "Désinsectisation", "label": "Fumigation",
     "texte": "Traitement par fumigation des locaux infestés. Mise en place du protocole de sécurité : évacuation des personnes et animaux, obturation des ouvertures. Diffusion du produit fumigène homologué. Aération de 4h minimum avant retour. Efficace sur l'ensemble des insectes présents."},

    # ══════════════════ DÉSINFECTION ══════════════════
    {"groupe": "Désinfection", "label": "Désinfection par nébulisation",
     "texte": "Désinfection générale des surfaces par nébulisation avec produit biocide homologué. Toutes les zones traitées (sol, murs, surfaces de contact). Aération obligatoire 2h minimum avant retour dans les locaux. Certificat de désinfection remis."},
    {"groupe": "Désinfection", "label": "Désinfection ciblée (zone contaminée)",
     "texte": "Désinfection ciblée des zones contaminées par projection de produit désinfectant. Ramassage et évacuation des déjections / cadavres dans des sacs hermétiques. Port des EPI adapté. Zone condamnée le temps du traitement."},
    {"groupe": "Désinfection", "label": "Désinfection virus",
     "texte": "Désinfection virale des locaux par nébulisation de produit biocide à spectre virucide homologué (norme EN 14476). Toutes les surfaces traitées. Temps de contact respecté. Aération 2h avant retour. Protocole conforme aux recommandations sanitaires en vigueur."},
    {"groupe": "Désinfection", "label": "Désinfection bactérienne",
     "texte": "Désinfection bactéricide des locaux et surfaces. Produit biocide bactéricide homologué (norme EN 1276 / EN 13697) appliqué par pulvérisation ou nébulisation. Surfaces de contact traitées en priorité. Locaux ventilés après intervention. Attestation remise."},
    {"groupe": "Désinfection", "label": "Syndrome Diogène",
     "texte": "Intervention dans le cadre d'un syndrome de Diogène. Évacuation et tri des déchets accumulés. Nettoyage en profondeur des surfaces, sols et murs. Désinfection et déodorisation de l'ensemble des pièces par nébulisation biocide. Port des EPI intégral. Déchets évacués selon réglementation. Rapport d'intervention transmis."},
    {"groupe": "Désinfection", "label": "Désinfection après décès",
     "texte": "Désinfection des locaux suite à découverte de corps / décès non assisté. Neutralisation des zones biologiquement contaminées. Application de produit biocide désinfectant et déodorisant. Évacuation des déchets biologiques dans des sacs conformes. Port des EPI de niveau 3. Intervention discrète et confidentielle."},
    {"groupe": "Désinfection", "label": "Locaux médicaux",
     "texte": "Désinfection de locaux médicaux, cabinet de soins ou établissement de santé. Protocole renforcé conforme aux exigences des établissements de santé. Produits biocides à spectre élargi (bactéricide, fongicide, virucide). Traitement de l'air et des surfaces. Traçabilité complète remise au responsable."},
    {"groupe": "Désinfection", "label": "Désinfection alimentaire",
     "texte": "Désinfection des locaux de production ou de stockage alimentaire. Produits biocides conformes aux normes agroalimentaires (sans rinçage / alimentaire). Traitement des surfaces de contact, équipements et zones de stockage. Respect des délais d'attente avant remise en production. Conforme aux exigences HACCP."},

    # ══════════════════ DÉPIGEONNAGE ══════════════════
    {"groupe": "Dépigeonnage", "label": "Pose de pics anti-pigeons",
     "texte": "Pose de pics anti-pigeons en acier inoxydable sur les zones de posage et de nidification (corniches, rebords, appuis de fenêtres, toiture). Nettoyage et désinfection préalable des surfaces souillées. Surface traitée transmise au client."},
    {"groupe": "Dépigeonnage", "label": "Pose de filets anti-pigeons",
     "texte": "Pose de filets anti-pigeons sur les ouvertures et zones d'accès. Fixation mécanique sécurisée. Surface couverte : à compléter (m²). Nettoyage des fientes réalisé avant installation. Entretien annuel recommandé."},
    {"groupe": "Dépigeonnage", "label": "Nettoyage et désinfection fientes",
     "texte": "Ramassage, nettoyage et désinfection des zones souillées par les fientes de pigeons. Port des EPI (masque FFP3, combinaison). Décontamination des surfaces à l'aide de produit biocide. Déchets évacués conformément à la réglementation."},
    {"groupe": "Dépigeonnage", "label": "Désinfection toiture",
     "texte": "Désinfection de la toiture et des abords souillés par les déjections de pigeons. Application de produit biocide fongicide et bactéricide sur les zones impactées. Port des EPI complet. Nids retirés et évacués. Recommandation de traitement préventif sur l'ensemble de la toiture."},
    {"groupe": "Dépigeonnage", "label": "Étanchéification des accès",
     "texte": "Identification et obturation des points d'accès utilisés par les pigeons (ouvertures, joints, fissures, passages de gaines). Pose de grillages ou de mousse expansive sur les zones de pénétration. Recommandations pour travaux de maçonnerie transmises au client. Suivi dans 30 jours."},

    # ══════════════════ TAUPE ══════════════════
    {"groupe": "Taupe", "label": "Pose de pièges taupiers",
     "texte": "Repérage des galeries actives par sondage. Pose de pièges taupiers aux galeries principales. Galeries traitées : à compléter. Retour prévu sous 48 à 72h pour relève des pièges. Remblayage des taupinières après traitement."},
    {"groupe": "Taupe", "label": "Relevé des pièges — résultat",
     "texte": "Relevé des pièges taupiers. Résultat : capture(s) constatée(s) / aucune capture (à compléter). Réajustement de la position des pièges si nécessaire. Surveillance recommandée sur les 15 jours suivants."},

    # ══════════════════ NID FRELONS ══════════════════
    {"groupe": "Nid frelons", "label": "Destruction nid de frelons",
     "texte": "Destruction du nid de frelons par injection d'insecticide à action foudroyante dans le nid. Nid retiré et éliminé après neutralisation complète. Sécurisation de la zone. Port des EPI complet obligatoire."},

    # ══════════════════ FRELONS ASIATIQUES ══════════════════
    {"groupe": "Frelons asiatiques", "label": "Destruction nid frelons asiatiques",
     "texte": "Traitement et destruction du nid de frelons asiatiques (Vespa velutina) par injection de produit insecticide homologué. Intervention réalisée en tenue de protection intégrale. Nid décroché et évacué. Aucun risque résiduel constaté."},
    {"groupe": "Frelons asiatiques", "label": "Intervention nacelle",
     "texte": "Destruction du nid de frelons asiatiques en hauteur, nécessitant l'utilisation d'une nacelle élévatrice. Traitement par injection d'insecticide homologué. Port des EPI intégral. Nid décroché et évacué après neutralisation complète. Zone sécurisée pendant l'intervention."},
    {"groupe": "Frelons asiatiques", "label": "Nid en hauteur",
     "texte": "Destruction du nid de frelons asiatiques situé en hauteur (arbre, façade, toiture). Accès sécurisé par échelle / perche télescopique / nacelle (à compléter). Injection d'insecticide dans le nid. Neutralisation complète avant décrochage. Nid éliminé. Zone dégagée."},
    {"groupe": "Frelons asiatiques", "label": "Nid en toiture",
     "texte": "Destruction du nid de frelons asiatiques installé en toiture / sous les tuiles / dans la charpente. Accès au comble sécurisé. Injection d'insecticide par les accès du nid. Neutralisation confirmée avant retrait. Obturation de l'entrée conseillée. Port des EPI complet."},
    {"groupe": "Frelons asiatiques", "label": "Nid en arbre",
     "texte": "Localisation et destruction du nid de frelons asiatiques dans un arbre. Traitement nocturne recommandé (moindre activité). Injection d'insecticide homologué dans le nid via perche ou directement. Nid décroché et éliminé après neutralisation. Zone sécurisée 24h."},
    {"groupe": "Frelons asiatiques", "label": "Intervention urgence",
     "texte": "Intervention d'urgence suite à signalement de nid de frelons asiatiques présentant un danger immédiat. Périmètre de sécurité mis en place. Traitement immédiat par injection d'insecticide. Nid neutralisé et retiré. Client informé des consignes de sécurité post-intervention."},

    # ══════════════════ GUÊPES ══════════════════
    {"groupe": "Guêpes", "label": "Destruction nid de guêpes",
     "texte": "Localisation et destruction du nid de guêpes par injection d'insecticide. Intervention en fin de journée (moindre activité). Port des EPI. Nid retiré et éliminé après neutralisation. Zone sécurisée."},

    # ══════════════════ ABEILLES ══════════════════
    {"groupe": "Abeilles", "label": "Récupération essaim d'abeilles",
     "texte": "Récupération de l'essaim d'abeilles en douceur et transfert vers un apiculteur partenaire. Aucun produit chimique utilisé. Intervention réalisée en tenue de protection apicole."},
    {"groupe": "Abeilles", "label": "Traitement colonie abeilles (bâtiment)",
     "texte": "Traitement de la colonie d'abeilles installée dans les parois/toiture. Injection d'insecticide dans les accès de la ruche sauvage. Obturation des entrées après traitement. Recommandation de contacter un apiculteur en amont si essaim récent."},
]

PRECONISATIONS_MODELES = [
    # ══════════════ DÉRATISATION ══════════════
    {"groupe": "Dératisation", "label": "Hygiène — denrées et nettoyage",
     "texte": "• Stocker les denrées alimentaires dans des contenants hermétiques.\n• Éviter les sacs poubelles ouverts.\n• Nettoyer les zones grasses et résidus alimentaires.\n• Supprimer les sources d'eau stagnante.\n• Nettoyer sous les équipements et meubles."},
    {"groupe": "Dératisation", "label": "Bâtiment — étanchéité et accès",
     "texte": "• Obturer les passages de canalisations.\n• Installer des grilles anti-rongeurs sur les orifices.\n• Reboucher fissures et trous dans les parois.\n• Vérifier et renforcer les bas de portes.\n• Poser des brosses ou plinthes anti-intrusion.\n• Contrôler faux plafonds et gaines techniques."},
    {"groupe": "Dératisation", "label": "Extérieur — environnement",
     "texte": "• Réduire les zones d'encombrement à l'extérieur.\n• Élaguer la végétation proche des bâtiments.\n• Éviter tout stockage extérieur non protégé.\n• Contrôler les regards et évacuations.\n• Fermer les accès aux locaux techniques."},
    {"groupe": "Dératisation", "label": "Prévention et suivi",
     "texte": "• Maintenir un contrat de suivi préventif régulier.\n• Réaliser des contrôles périodiques des postes.\n• Cartographier les postes d'appâtage.\n• Former le personnel aux bonnes pratiques et à la détection précoce."},
    {"groupe": "Dératisation", "label": "Colmatage des points d'entrée",
     "texte": "Colmatage urgent des points d'entrée identifiés (gaines, fissures, passages de tuyauteries). Intervention maçonnerie ou pose de grillage anti-rongeurs recommandée. Sans cette action, le traitement restera insuffisant."},
    {"groupe": "Dératisation", "label": "Amélioration stockage alimentaire",
     "texte": "Améliorer les conditions de stockage : produits alimentaires dans des contenants hermétiques (inox ou plastique rigide), surélevés du sol (minimum 15 cm). Supprimer tout accès à la nourriture et à l'eau pour les rongeurs."},
    {"groupe": "Dératisation", "label": "Plan de contrôle mensuel",
     "texte": "Mise en place d'un plan de contrôle mensuel recommandée. Passage régulier du technicien pour relevé des postes et ajustement du dispositif. Indispensable pour maintenir le site sous contrôle et répondre aux exigences HACCP."},
    {"groupe": "Dératisation", "label": "Formation du personnel",
     "texte": "Formation du personnel à la détection des signes d'activité rongeurs (fèces, gnures, traces, bruits nocturnes). Signalement immédiat à la direction en cas de constat. Tenue d'un registre des observations recommandée."},
    {"groupe": "Dératisation", "label": "Nettoyage zones de stockage",
     "texte": "Nettoyage en profondeur des zones de stockage et locaux techniques recommandé. Élimination des déchets, cartons et matériaux accumulés qui constituent des sites de nidification. Fréquence : minimum mensuelle."},
    {"groupe": "Dératisation", "label": "Inspection des réseaux",
     "texte": "Inspection des réseaux d'évacuation et siphons de sol recommandée. Pose de clapets anti-refoulement sur les évacuations si remontée de rongeurs par les canalisations suspectée."},

    # ══════════════ DÉSINSECTISATION ══════════════
    {"groupe": "Désinsectisation", "label": "Cafards / Blattes — mesures correctives",
     "texte": "• Nettoyage approfondi des graisses sous équipements.\n• Réduction de l'humidité dans les locaux sensibles.\n• Éviter la vaisselle stagnante et les résidus alimentaires.\n• Contrôle et entretien des siphons d'évacuation.\n• Obturation des fissures et joints décollés.\n• Éliminer les cartons inutiles (sites de refuge).\n• Nettoyer les moteurs des appareils frigorifiques."},
    {"groupe": "Désinsectisation", "label": "Punaises de lit — protocole client",
     "texte": "• Lavage de tous les textiles à haute température (60°C minimum).\n• Limiter les déplacements d'objets contaminés vers d'autres pièces.\n• Aspiration minutieuse des matelas, sommiers, plinthes et recoins.\n• Traitement simultané des pièces adjacentes recommandé.\n• Utilisation de housses anti-punaises sur matelas et sommiers.\n• Contrôle visuel à réaliser 2 semaines après intervention."},
    {"groupe": "Désinsectisation", "label": "Fourmis — prévention",
     "texte": "• Supprimer les résidus sucrés et miellats (nettoyage régulier).\n• Étanchéifier les accès extérieurs (joints, fissures).\n• Contrôler l'humidité dans les zones touchées.\n• Détruire les pistes de phéromones avec un produit adapté."},
    {"groupe": "Désinsectisation", "label": "Mouches — mesures préventives",
     "texte": "• Installer des moustiquaires sur les ouvertures.\n• Nettoyer régulièrement les zones organiques (bacs à compost, poubelles).\n• Contrôler et entretenir les évacuations et siphons.\n• Installer des destructeurs d'insectes UV dans les locaux sensibles.\n• Éviter les déchets alimentaires exposés."},
    {"groupe": "Désinsectisation", "label": "Moustiques — gîtes larvaires",
     "texte": "• Supprimer toutes les eaux stagnantes (soucoupes, bâches, pneus).\n• Nettoyer régulièrement les gouttières.\n• Couvrir les récupérateurs d'eau de pluie.\n• Entretenir les espaces verts et bassins."},
    {"groupe": "Désinsectisation", "label": "Puces — traitement global",
     "texte": "• Traiter les animaux de compagnie simultanément (vétérinaire).\n• Aspirer soigneusement les textiles, tapis et coussins.\n• Nettoyage vapeur des sols et mobilier recommandé.\n• Répéter le traitement si nécessaire à J+15 (cycle biologique)."},
    {"groupe": "Désinsectisation", "label": "Renforcement hygiène locaux",
     "texte": "Renforcement de l'hygiène des locaux indispensable : nettoyage quotidien des plans de travail, évacuations et joints. Élimination des résidus alimentaires. Sans amélioration des conditions d'hygiène, le risque de réinfestation est élevé."},
    {"groupe": "Désinsectisation", "label": "Traitement préventif trimestriel",
     "texte": "Traitement préventif trimestriel recommandé pour maintenir le site sous contrôle. Un passage régulier permet d'agir avant tout développement de foyer d'infestation."},
    {"groupe": "Désinsectisation", "label": "Remplacement des joints cuisine",
     "texte": "Remplacement des joints de cuisine et de plomberie endommagés ou décollés recommandé. Les joints dégradés constituent des refuges idéaux pour les cafards et blattes. Intervention plombier / carreleur conseillée."},
    {"groupe": "Désinsectisation", "label": "Nettoyage des évacuations",
     "texte": "Nettoyage régulier des évacuations, siphons et grilles d'égout recommandé (minimum mensuel). Les dépôts organiques favorisent la prolifération des insectes. Utilisation d'un désinfectant adapté."},
    {"groupe": "Désinsectisation", "label": "Remplacement des pièges de contrôle",
     "texte": "Remplacement régulier des pièges de contrôle (plaques engluantes) tous les 2 mois ou dès saturation. Ces pièges permettent de détecter précocement toute réapparition d'infestation."},
    {"groupe": "Désinsectisation", "label": "Lavage des textiles (punaises)",
     "texte": "Lavage de l'ensemble des textiles à 60°C minimum (draps, taies, rideaux, vêtements). Passage de l'aspirateur sur les matelas, sommiers et plinthes. Mise en sac hermétique des textiles non lavables pendant 72h minimum."},

    # ══════════════ DÉSINFECTION ══════════════
    {"groupe": "Désinfection", "label": "Après désinfection — consignes",
     "texte": "• Respecter le temps de réentrée indiqué sur la fiche technique.\n• Aérer les locaux avant retour des occupants.\n• Nettoyer les surfaces critiques régulièrement.\n• Désinfecter les points de contact fréquents (poignées, interrupteurs).\n• Utiliser des produits homologués conformes aux normes EN.\n• Mettre en place un protocole sanitaire documenté."},
    {"groupe": "Désinfection", "label": "Prévention sanitaire",
     "texte": "• Renforcer le lavage des mains (savon bactéricide).\n• Port des EPI adaptés dans les zones à risque.\n• Gestion des déchets DASRI si nécessaire.\n• Contrôle et entretien de la ventilation et VMC.\n• Nettoyage fréquent des sanitaires et points de contact."},
    {"groupe": "Désinfection", "label": "Agroalimentaire / HACCP",
     "texte": "• Respect strict de la chaîne du froid.\n• Désinfection quotidienne des zones de production alimentaire.\n• Traçabilité des interventions de désinfection (registre).\n• Contrôle microbiologique régulier des surfaces.\n• Plan de nettoyage et désinfection documenté et affiché."},
    {"groupe": "Désinfection", "label": "Suivi médical post-intervention",
     "texte": "• Consultation médicale recommandée pour toute personne exposée avant désinfection.\n• En cas de symptômes (nausées, fièvre, troubles respiratoires), consulter un médecin rapidement.\n• Conserver le rapport d'intervention pour le dossier médical si nécessaire."},
    {"groupe": "Désinfection", "label": "Renouvellement traitement",
     "texte": "Renouvellement du traitement de désinfection recommandé dans 3 mois. Mise en place d'un protocole de désinfection régulière des surfaces de contact (journalier ou hebdomadaire selon usage)."},
    {"groupe": "Désinfection", "label": "Protocole désinfection quotidienne",
     "texte": "Mise en place d'un protocole de désinfection quotidienne des surfaces de contact recommandée. Utilisation d'un produit désinfectant homologué. Formation du personnel aux bonnes pratiques d'hygiène."},
    {"groupe": "Désinfection", "label": "Aération et ventilation",
     "texte": "Amélioration de la ventilation des locaux recommandée. Une bonne aération réduit le développement bactérien et fongique. Vérification et entretien des grilles de ventilation et VMC."},
    {"groupe": "Désinfection", "label": "Suivi médical (après décès)",
     "texte": "Suivi médical recommandé pour toute personne ayant été exposée à la zone avant désinfection. Consultation d'un médecin en cas de symptômes (nausées, fièvre, troubles respiratoires). Conservation du rapport d'intervention pour dossier."},

    # ══════════════ TAUPE ══════════════
    {"groupe": "Taupe", "label": "Suivi et prévention — terrain",
     "texte": "• Identifier et surveiller les galeries actives régulièrement.\n• Limiter l'arrosage excessif du sol (favorise l'activité des taupes).\n• Réduire la présence de vers blancs (larves) dans le sol.\n• Contrôler l'humidité des sols.\n• Éviter les vibrations excessives après pose des pièges.\n• Maintenir un suivi sur plusieurs semaines.\n• Reboucher les anciennes galeries après neutralisation."},
    {"groupe": "Taupe", "label": "Surveillance sur 30 jours",
     "texte": "Surveillance du terrain sur 30 jours recommandée. Signalement immédiat de toute nouvelle taupinière. En cas de réactivité, nouveau cycle de pièges à programmer rapidement."},
    {"groupe": "Taupe", "label": "Traitement préventif de la pelouse",
     "texte": "Traitement préventif recommandé en période d'activité intense (printemps / automne). Un suivi saisonnier permet de limiter les dégâts sur les espaces verts."},

    # ══════════════ DÉPIGEONNAGE ══════════════
    {"groupe": "Dépigeonnage", "label": "Prévention des accès",
     "texte": "• Installer des pics anti-pigeons sur les zones de posage.\n• Poser des filets de protection sur les ouvertures.\n• Fermer les accès aux combles et greniers.\n• Étanchéifier les ouvertures de toiture.\n• Installer des fils tendus si nécessaire (grandes surfaces)."},
    {"groupe": "Dépigeonnage", "label": "Hygiène et nettoyage",
     "texte": "• Nettoyer les fientes régulièrement (risque sanitaire).\n• Désinfecter les zones contaminées après nettoyage.\n• Utiliser des protections respiratoires (masque FFP3) lors du nettoyage.\n• Éviter l'accumulation de déchets alimentaires à proximité."},
    {"groupe": "Dépigeonnage", "label": "Prévention durable",
     "texte": "• Interdire strictement le nourrissage des oiseaux sur le site.\n• Contrôle régulier des installations anti-pigeons.\n• Maintenance annuelle recommandée (vérification fixations, remplacement éléments dégradés)."},
    {"groupe": "Dépigeonnage", "label": "Inspection annuelle des dispositifs",
     "texte": "Inspection annuelle des dispositifs anti-pigeons recommandée. Vérification de l'état des pics, filets et fixations. Remplacement des éléments endommagés. Nettoyage préventif des zones traitées."},
    {"groupe": "Dépigeonnage", "label": "Traitement toiture complet",
     "texte": "Traitement anti-pigeon complet de la toiture recommandé. L'intervention ponctuelle n'empêche pas les pigeons de se redéployer sur d'autres zones. Un traitement global assure une protection durable."},
    {"groupe": "Dépigeonnage", "label": "Nettoyage trimestriel",
     "texte": "Nettoyage trimestriel des zones souillées par les fientes recommandé. Les fientes de pigeon sont corrosives et présentent un risque sanitaire (histoplasmose). Désinfection systématique après chaque nettoyage."},
    {"groupe": "Dépigeonnage", "label": "Obturation des accès toiture",
     "texte": "Obturation de l'ensemble des accès à la toiture (lucanes, chatières, joints de rives) recommandée pour empêcher la nidification. Intervention couvreur / maçon à prévoir. Devis sur demande."},

    # ══════════════ FRELONS / GUÊPES ══════════════
    {"groupe": "Frelons asiatiques", "label": "Sécurité et prévention nids",
     "texte": "• Éviter toute approche du nid avant traitement professionnel.\n• Sécuriser le périmètre autour du nid (5m minimum).\n• Prévoir l'intervention tôt le matin ou la nuit (activité réduite).\n• Contrôler régulièrement toiture et arbres en période estivale.\n• Vérifier l'absence de nids secondaires dans un périmètre de 500m.\n• Informer l'ensemble des occupants avant toute intervention."},
    {"groupe": "Frelons asiatiques", "label": "Surveillance du secteur",
     "texte": "Surveillance du secteur recommandée : les frelons asiatiques construisent généralement plusieurs nids secondaires dans un périmètre de 500m. Signalement de tout nid suspect au technicien."},
    {"groupe": "Frelons asiatiques", "label": "Signalement à la mairie",
     "texte": "Signalement à la mairie ou à la DDT recommandé (Vespa velutina est une espèce invasive). Certaines communes disposent d'un service de traitement gratuit ou subventionné."},
    {"groupe": "Frelons asiatiques", "label": "Ne pas obstruer l'entrée du nid",
     "texte": "Ne pas obstruer l'entrée du nid avant traitement complet. Les frelons enfermés deviennent agressifs et peuvent percer les cloisons. Périmètre de sécurité de 5m à respecter jusqu'à neutralisation confirmée."},
    {"groupe": "Guêpes", "label": "Prévention et sécurité",
     "texte": "• Éviter de laisser des aliments sucrés ou boissons à l'air libre.\n• Obturer le site de nidification après neutralisation complète (48h).\n• Couvrir les poubelles et nettoyer les zones de repas extérieures.\n• Signalement de tout nouveau nid au technicien pour intervention rapide."},
    {"groupe": "Guêpes", "label": "Obturation du site après traitement",
     "texte": "Obturation du site de nidification recommandée après confirmation de la neutralisation du nid (48h). Empêche toute réinstallation dans le même endroit la saison suivante."},
    {"groupe": "Guêpes", "label": "Éviter les odeurs sucrées",
     "texte": "Éviter de laisser des aliments sucrés ou boissons à l'air libre en extérieur. Les guêpes sont attirées par les odeurs sucrées et les protéines. Couvrir les poubelles et nettoyer les zones de repas."},

    # ══════════════ ABEILLES ══════════════
    {"groupe": "Abeilles", "label": "Récupération et protection",
     "texte": "• Favoriser la récupération de l'essaim par un apiculteur (espèce protégée).\n• Éviter la destruction chimique si un apiculteur peut intervenir.\n• Contacter un apiculteur local avant toute décision de traitement.\n• Sécuriser l'accès au public pendant la présence de l'essaim.\n• Contrôler l'absence de réinstallation d'une colonie après intervention."},
    {"groupe": "Abeilles", "label": "Contact apiculteur recommandé",
     "texte": "Contact avec un apiculteur local fortement recommandé avant toute intervention chimique sur un essaim d'abeilles. Les abeilles sont des espèces protégées et utiles. La récupération par un apiculteur est préférable au traitement."},
    {"groupe": "Abeilles", "label": "Obturation après traitement",
     "texte": "Obturation de l'accès à la cavité après traitement (minimum 72h après intervention). Risque de réinstallation d'un nouvel essaim si l'entrée reste ouverte. Restes de cire à retirer si accessible (risque de fusion en été et dégâts aux parois)."},
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

# ─── PLANS D'APPÂTAGE ────────────────────────────────────────────────────────

@app.route('/clients/<int:id>/plans/ajouter', methods=['POST'])
@login_required
def plan_ajouter(id):
    c = Client.query.get_or_404(id)
    file = request.files.get('plan_file')
    if not file or not file.filename:
        flash('Aucun fichier sélectionné.', 'warning')
        return redirect(url_for('client_detail', id=id))
    mime = file.mimetype or 'application/octet-stream'
    if not (mime.startswith('image/') or mime == 'application/pdf'):
        flash('Format accepté : image (JPG, PNG, WEBP) ou PDF.', 'danger')
        return redirect(url_for('client_detail', id=id))
    file.seek(0, 2); size = file.tell(); file.seek(0)
    if size > 15 * 1024 * 1024:
        flash('Fichier trop lourd (max 15 Mo).', 'danger')
        return redirect(url_for('client_detail', id=id))
    nom = request.form.get('plan_nom', '').strip() or file.filename
    notes = request.form.get('plan_notes', '').strip()
    data_b64 = base64.b64encode(file.read()).decode('utf-8')
    plan = PlanAppatage(client_id=id, nom=nom, notes=notes,
                        data=data_b64, mimetype=mime)
    db.session.add(plan)
    db.session.commit()
    flash(f'Plan « {nom} » ajouté.', 'success')
    return redirect(url_for('client_detail', id=id) + '#plans-appatage')

@app.route('/clients/plans/<int:plan_id>')
@login_required
def plan_voir(plan_id):
    p = PlanAppatage.query.get_or_404(plan_id)
    data = base64.b64decode(p.data)
    ext = 'pdf' if p.mimetype == 'application/pdf' else p.nom.rsplit('.',1)[-1] if '.' in p.nom else 'jpg'
    return send_file(io.BytesIO(data), mimetype=p.mimetype,
                     download_name=f"plan_{p.client_id}_{p.id}.{ext}")

@app.route('/clients/plans/<int:plan_id>/supprimer', methods=['POST'])
@login_required
def plan_supprimer(plan_id):
    p = PlanAppatage.query.get_or_404(plan_id)
    cid = p.client_id
    nom = p.nom
    db.session.delete(p)
    db.session.commit()
    flash(f'Plan « {nom} » supprimé.', 'info')
    return redirect(url_for('client_detail', id=cid) + '#plans-appatage')


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
    sig_data = request.form.get('signature_data', '').strip()
    if not sig_data:
        flash('Veuillez apposer votre signature avant de valider.', 'warning')
        return redirect(url_for('portail_dashboard', token=token))
    b.signature_image  = sig_data
    b.signature_client = True
    b.date_signature   = datetime.utcnow()
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
            "ALTER TABLE bons_intervention ADD COLUMN IF NOT EXISTS signature_image TEXT",
            "ALTER TABLE bons_intervention ADD COLUMN IF NOT EXISTS signature_technicien TEXT",
            "ALTER TABLE clients ADD COLUMN IF NOT EXISTS notifs_actives BOOLEAN DEFAULT TRUE",
            "ALTER TABLE clients ADD COLUMN IF NOT EXISTS sms_actif BOOLEAN DEFAULT FALSE",
            """CREATE TABLE IF NOT EXISTS plans_appatage (
                id SERIAL PRIMARY KEY,
                client_id INTEGER NOT NULL REFERENCES clients(id),
                nom VARCHAR(200) DEFAULT 'Plan',
                notes TEXT,
                data TEXT NOT NULL,
                mimetype VARCHAR(80) DEFAULT 'image/jpeg',
                created_at TIMESTAMP DEFAULT NOW()
            )""",
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
