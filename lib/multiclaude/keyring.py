"""Keys that let an app outside Telegram speak for a person: a Siri shortcut (the siri add-in) or
OwnTracks (the locations add-in). The relay passes on only a key's SHA-256, and so are the keys
kept: a JSON file {hash: name}, one key per person, which is printed once when it's made."""
import hashlib
import secrets

from multiclaude import jsonfile


def digest(key):
    return hashlib.sha256(key.encode()).hexdigest()


class KeyRing:
    """The keys kept in one file."""

    def __init__(self, path):
        self.path = path

    def new_key(self, name):
        """A fresh key for name, replacing the one they had; only its hash is kept."""
        key = secrets.token_urlsafe(32)

        def replace(keys):
            drop_keys_of(keys, name)
            keys[digest(key)] = name

        jsonfile.edit_object(self.path, replace)
        return key

    def revoke(self, name):
        """Forget name's key; False if they had none."""
        return jsonfile.edit_object(self.path, lambda keys: drop_keys_of(keys, name), create=False)

    def has_key(self, name):
        return name in jsonfile.read_object(self.path).values()

    def owner(self, key_hash):
        """Whose key has this SHA-256 hash, or None."""
        return jsonfile.read_object(self.path).get(key_hash) if key_hash else None


def drop_keys_of(keys, name):
    for hashed in [h for h, owner in keys.items() if owner == name]:
        del keys[hashed]
