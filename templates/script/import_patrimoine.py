# -*- coding: utf-8 -*-
"""
Import du patrimoine (liste de logements) d'un bailleur social dans la base
de l'app agenda-interventions.

Usage :
    python scripts/import_patrimoine.py chemin/vers/patrimoine.csv \
        --client "AMSOM Habitat" \
        [--replace]

- Le CSV doit être celui envoyé par le bailleur (encodage Latin-1, séparateur
  ';', avec des lignes d'en-tête décoratives avant la vraie ligne de colonnes
  qui commence par "SECTEUR").
- Si le client n'existe pas encore (recherche par nom/société), il est créé
  automatiquement en tant que client "professionnel".
- Par défaut le script AJOUTE les lignes. Avec --replace, il supprime d'abord
  toutes les lignes de patrimoine déjà associées à ce client avant de
  réimporter (utile si le bailleur envoie une liste mise à jour).

Lancer ce script depuis la racine du repo, avec le même environnement
(.env / DATABASE_URL) que l'application.
"""
import argparse
import csv
import io
import sys

# On réutilise l'app Flask, la config DB et les modèles existants
from app import app, db, Client, PatrimoineLogement


def read_rows(csv_path):
    """Lit le CSV en trouvant automatiquement la vraie ligne d'en-tête
    (celle qui commence par SECTEUR), pour ignorer les lignes décoratives
    du début envoyées par certains bailleurs."""
    with open(csv_path, encoding='latin-1', newline='') as f:
        raw_lines = f.readlines()

    header_idx = None
    for i, line in enumerate(raw_lines):
        if line.strip().upper().startswith('SECTEUR'):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(
            "Impossible de trouver la ligne d'en-tête (colonne 'SECTEUR'). "
            "Vérifie le format du fichier."
        )

    reader = csv.DictReader(io.StringIO(''.join(raw_lines[header_idx:])), delimiter=';')
    return list(reader)


def get_or_create_client(nom_client):
    client = Client.query.filter(
        db.or_(Client.societe == nom_client, Client.nom == nom_client)
    ).first()
    if client:
        print(f"Client existant trouvé : #{client.id} — {client.nom_affichage}")
        return client

    client = Client(
        nom=nom_client,
        societe=nom_client,
        type_client='professionnel',
        actif=True,
    )
    db.session.add(client)
    db.session.commit()
    print(f"Client créé : #{client.id} — {nom_client}")
    return client


def main():
    parser = argparse.ArgumentParser(description="Import du patrimoine logements CSV")
    parser.add_argument('csv_path', help="Chemin vers le fichier CSV")
    parser.add_argument('--client', required=True, help="Nom du client bailleur (ex: 'AMSOM Habitat')")
    parser.add_argument('--replace', action='store_true',
                         help="Supprime les logements existants de ce client avant import")
    args = parser.parse_args()

    with app.app_context():
        client = get_or_create_client(args.client)

        if args.replace:
            nb = PatrimoineLogement.query.filter_by(client_id=client.id).delete()
            db.session.commit()
            print(f"{nb} ancienne(s) ligne(s) supprimée(s) pour ce client.")

        rows = read_rows(args.csv_path)
        print(f"{len(rows)} ligne(s) lue(s) dans le CSV.")

        count = 0
        batch = []
        for row in rows:
            # Ignore les lignes vides / lignes de séparation résiduelles
            if not row.get('SECTEUR') and not row.get('BÂTIMENT'):
                continue
            batch.append(PatrimoineLogement(
                client_id=client.id,
                secteur=row.get('SECTEUR'),
                programme=row.get('PROGRAMME'),
                tranche=row.get('TRANCHE'),
                code_batiment=row.get('Code bâtiment'),
                batiment=row.get('BÂTIMENT'),
                code_escalier=row.get('Code Escalier'),
                numero_voirie=row.get('Numéro de Voirie'),
                voirie=row.get('VOIRIE'),
                module=row.get('MODULE'),
                numero_logement=row.get('Numéro du logement'),
                etage=row.get('ETAGE'),
                commune=row.get('COMMUNE'),
                type_logement=row.get('Type'),
                nature_batiment=row.get('Nature Bâtiment'),
            ))
            count += 1
            if len(batch) >= 500:
                db.session.bulk_save_objects(batch)
                db.session.commit()
                batch = []
                print(f"  ... {count} lignes importées")

        if batch:
            db.session.bulk_save_objects(batch)
            db.session.commit()

        print(f"Import terminé : {count} logement(s) importé(s) pour le client #{client.id}.")


if __name__ == '__main__':
    main()
