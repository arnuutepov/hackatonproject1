"""Local owner CLI. Passwords are read interactively, never from command arguments."""
import argparse
import getpass
import sqlite3
from support_copilot.auth_store import AuthStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create', 'reset-password', 'list'])
    parser.add_argument('--username')
    parser.add_argument('--role', choices=['agent', 'lead', 'admin'], default='agent')
    args = parser.parse_args()
    store = AuthStore()
    if args.action == 'list':
        for user in store.users():
            print(user['username'], user['role'], 'active' if user['active'] else 'disabled')
        return
    if not args.username:
        parser.error('--username is required')
    first = getpass.getpass('Password (15..256 characters): ')
    if first != getpass.getpass('Repeat password: '):
        parser.error('Passwords do not match')
    try:
        if args.action == 'create':
            store.create_user(args.username, first, args.role, actor='local_cli')
        else:
            store.reset_password(args.username, first)
    except (ValueError, sqlite3.IntegrityError):
        parser.error('Invalid credentials, unknown operator or duplicate username')
    print('Operator saved. No password or session token was printed.')


if __name__ == '__main__':
    main()
