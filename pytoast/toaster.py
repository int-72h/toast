#!/usr/bin/python

# A Change is an operation to a filesystem, such as writen file, or deleted one.
# A Changeset is a list of changes. Cumulative changes are a list of changes
# that are the result of applying a range of changesets (referred to as playback).
# In practice, it represents a complete filesystem and is used as such.
#
# Typing is used heavily because correctness of objects is a massive concern,
# and unfortunately Python is a bit flimsy in that regard.
#
# The filesystem/format is as follows, assuming "tvs" is the root:
# 
# tvs/cumlcache contains a cache of the cumulative changeset of the latest
# revision. This is used for internal processing, is not a part of the spec, 
# and should not be made accessible to the client.
#
# tvs/objects/ contains file objects, each with a unique ID. Changes refer to
# these files mapped to a path. The ID format is an arbitrary ASCII string
# (in this implementation it's a UUID.) Objects ending in ".sig" are reserved
# for signatures. This isn't likely to be implemented since objects are likely 
# going to be served over a secure connection anyways.
# 
# tvs/revisions/ contains sequential enumerated files representing revisions.
# Each of them contains an array of Change, which has information stored in
# JSON.
#
# "path" is the relative path to the root of the installation. It must use
# forward slashes, and cannot start with "./", and must be POSIX compliant. 
# For example: "resources/merc.png". "type" represents the type of operation as a number.
# Zero is write representing added and modified files, one represents new directories, and two represents a
# deleted file/directory.
# "object" is the ID of the object avaliable under tvs/objects. May be omitted or
# null if the type is a created directory.
# "hash" is the md5 hash of the object, used for checking for incidential file
# corruption and internally for comparing files.

from . import *

import os
import pathlib
import shutil
import uuid
import posixpath
import sys
import hashlib
import json
import tqdm
import zipfile

TYPE_WRITE = 0
TYPE_MKDIR = 1
TYPE_DELETE = 2


# Compares two cumulative changesets (which is used as a representation of
# the filesystem) and generates a changeset with the differences between
# the two.
def compare_cuml_changes(old, new):
    oldmap = changes_to_map(old)
    newmap = changes_to_map(new)
    changes = []
    if new is not None:
        for x in new:
            if x["path"] not in oldmap or x["path"] != TYPE_MKDIR and oldmap[x["path"]]["hash"] != x["hash"]:
                changes.append(x)

    if old != None:
        for x in old:
            if x["path"] not in newmap:
                changes.append(invert_change(x))

    return changes


# Converts a filesystem to a cumulative changelist.
def fs_to_accu_changes(path):
    changes = []

    def errhandler(exception):
        print(exception, file=sys.stderr)
        exit(1)

    for dirpath, directories, files in tqdm.tqdm(os.walk(path, onerror=errhandler)):
        for name in files:
            b = open(posixpath.join(dirpath, name), 'rb').read()
            hash = hashlib.md5(b)
            changes.append({
                "type": TYPE_WRITE,
                "path": posixpath.relpath(posixpath.join(dirpath, name), path),
                "hash": hash.hexdigest(),
                "object": None
            })

        for name in directories:
            changes.append({
                "type": TYPE_MKDIR,
                "path": posixpath.relpath(posixpath.join(dirpath, name), path),
                "hash": None,
                "object": None
            }
            )

    return changes


def read_file(path):
    file = open(path, "r")
    revision = json.load(file)
    return revision


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("source",help="The source directory.")
    parser.add_argument("tvndir",help="The target TVN directory, i.e the files that will be served.")
    parser.add_argument("-z", "--zip", help="[rei] Generate a zip used for fresh installs.", action="store_true")
    parser.add_argument("-t", "--tag", help="Arbitrary name (tag) for the revision. Semver reccomended.")
    args = parser.parse_args()
    tvsdir = pathlib.PosixPath(args.tvndir)
    srcfs = pathlib.PosixPath(args.source)
    # Make the tvs directories if they don't exist.
    os.umask(0)
    os.makedirs(tvsdir / 'objects', 0o777, exist_ok=True)
    os.makedirs(tvsdir / 'revisions', 0o777, exist_ok=True)

    cumlcache = []
    head_version = 0
    write_count = 0
    # Try to open the cumlcache
    try:
        file = open(tvsdir / 'cumlcache', "r")
        cumlcache = json.load(file)
        head_version = cumlcache["version"] + 1
        file.close()
    # If it doesn't exist, or it's empty, generate it.
    except FileNotFoundError:
        print("Regenerating the cumulative cache...", file=sys.stderr)
        (tvsdir / 'cumlcache').touch()
        i = 0
        revisions = []
        # Read files under tvn/revisions number by number until
        # we reach a revision that doesn't exist.
        while True:
            dir = tvsdir / 'revisions' / str(i)
            if not os.path.isfile(dir):
                break

            revision = read_file(dir)

            revisions.append(revision)

            i = i + 1

        head_version = i

        cumlcache = {
            "revision": head_version - 1,
            "changes": replay_changes_nodel(revisions)
        }

    print("Reading from file system...", file=sys.stderr)
    fscuml = fs_to_accu_changes(srcfs)
    print("Comparing changes...", file=sys.stderr)
    changes = compare_cuml_changes(cumlcache["changes"], fscuml)
    if len(changes) == 0:
        print("No changes found.", file=sys.stderr)
        exit(0)

    print("Copying objects...", file=sys.stderr)
    # Iterate over new revision
    for x in changes:
        # Print changes to stdout for piping.
        if x["type"] is TYPE_WRITE:
            print("W ", end='')
            write_count += 1
        if x["type"] is TYPE_MKDIR:
            print("F ", end='')
        if x["type"] is TYPE_DELETE:
            print("D ", end='')

        print(x["path"])

        # Populate changes with uuids and copy files to tvs/objects/.
        if x["type"] is TYPE_WRITE:
            object_id = str(uuid.uuid4()).replace('-', '')
            shutil.copy2(srcfs / x["path"], tvsdir / 'objects' / object_id)
            x["object"] = object_id

    # Save new revision
    # changes.append(head_version) what was this line for?!
    new_version_dir = tvsdir / 'revisions' / str(head_version)
    new_version_dir.touch(0o777)
    file = open(new_version_dir, "w")
    towrite = {
        "changes": changes,
        "revision": head_version
    }
    if args.tag:
        towrite["tag"] = args.tag
    print("Tag: " + args.tag)
    json.dump(towrite, file)
    file.close()
    # Update cache
    cache_dir = tvsdir / 'cumlcache'
    cache_dir.touch(0o777)
    cumlcache = {
        "version": head_version,
        "changes": replay_changes_nodel([cumlcache["changes"], changes])
    }
    file = open(cache_dir, "w")
    json.dump(dict(cumlcache), file)
    (tvsdir / "revisions" / "latest").touch()
    file = open(tvsdir / "revisions" / "latest", "w")
    file.write(str(head_version))
    file.close()
    if args.zip:  # ok we need to zip from last and latest if this isn't revision zero or one (that'd be the same then.)
        # target_path = tvsdir / 'rei'/ 'from' / '0' / 'to' / (str(head_version) + ".zip") object zipping at some point
        target_path = tvsdir  /  "rei" / "latest.zip"
        os.makedirs(target_path.parents[0], 0o777, exist_ok=True)
        print("Writing zip...")
        with zipfile.ZipFile(target_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for entry in srcfs.rglob("*"):
                zip_file.write(entry, entry.relative_to(srcfs))


# Update cache
if __name__ == "__main__":
    main()