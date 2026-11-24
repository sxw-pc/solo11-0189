"""Current conservation scan upload counting."""

def uploads(rows):
    return len({row['upload_id'] for row in rows})
