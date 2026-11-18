import logging
import os
import shutil
import subprocess
import time
import traceback

import pygit2


def delete_repo_clone(repo_clone: pygit2.Repository):
    workdir = getattr(repo_clone, "workdir", None)
    repo_clone.free()
    if workdir and os.path.exists(workdir):
        try:
            shutil.rmtree(workdir)
        except Exception:
            # Under rootless Podman, container-created files belong to subuids
            res = subprocess.run(f"podman unshare rm -rf '{workdir}'", shell=True, capture_output=True)
            if res.returncode != 0 and os.path.exists(workdir):
                subprocess.run(f"rm -rf '{workdir}'", shell=True)


def clone_repo(clone_url: str, path: str) -> pygit2.Repository:
    retries = 3
    for r in range(retries):
        try:
            repo_clone: pygit2.Repository = pygit2.clone_repository(clone_url, path)
            return repo_clone
        except pygit2.GitError as e:
            if r == retries - 1:
                logging.error(
                    f"Error while cloning {clone_url}: {traceback.format_exc()}"
                )
                raise e


def git_clean(repo: pygit2.Repository, force=True):
    # Get the status of the working directory
    status = repo.status()

    # Iterate over the status entries
    for filepath, status_flags in status.items():
        # Check if the file is untracked
        if status_flags == pygit2.GIT_STATUS_WT_NEW:
            full_path = os.path.join(repo.path, "..", filepath)

            try:
                if os.path.isdir(full_path) and not os.path.islink(full_path):
                    if force:
                        # Remove directory recursively
                        shutil.rmtree(full_path)
                else:
                    # Remove file or symbolic link
                    os.remove(full_path)
            except Exception as e:
                logging.error(f"Error removing {full_path}: {e}")
