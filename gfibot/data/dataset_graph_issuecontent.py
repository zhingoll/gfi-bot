import os
import re
import nltk
import logging
import textstat
import argparse
import mongoengine
import numpy as np
import multiprocessing as mp

from typing import Union
from collections import Counter
from dateutil.parser import parse as parse_date
from gfibot import CONFIG
from gfibot.collections import *
from mongoengine.queryset.visitor import Q

logger = logging.getLogger(__name__)



def _delete_code_snippets(s: str) -> str:
    if s is None:
        return ""
    p = re.compile(r"```.*?```", flags=re.S)
    s = p.sub("", s)
    # return " ".join(s.split())
    return s


def _delete_urls(s: str) -> str:
    if s == None:
        return ""
    p = re.compile(r"https?://[\w\.-]+(?:\:\d+)?(?:/[\w\./?%&=\-]*)?")
    s = p.sub("", s)
    # return " ".join(s.split())
    return s


def update_issue_content(issue: [RepoIssue]) -> IssueContent:
    """ """
    query = Q(owner=issue.owner, name=issue.name, number=issue.number)
    existing = IssueContent.objects(query)
    if existing.count() > 0:
        logger.info(
            f"{issue.owner}/{issue.name}#{issue.number}: Already in dataset"
        )
        return existing.first()

    repo_issue: RepoIssue = RepoIssue.objects(query).first()
    # 这个函数只关心issue
    if repo_issue.is_pull == True:
        logger.error(f"{issue.owner}/{issue.name}#{issue.number}: Pull Request")
        return
    clean_body = _delete_urls(_delete_code_snippets(repo_issue.body))

    data = IssueContent()

    data.owner = issue.owner
    data.name = issue.name
    data.number = issue.number
    data.title = repo_issue.title
    data.body = clean_body

    data.save()
    return data


def update_dataset_with_issues(
    all_issues: List[RepoIssue]
):
    for i, issue in enumerate(all_issues):
        # existing = Issue_Content.objects(name=issue.name, owner=issue.owner, number=issue.number)
        # if existing.count() > 0:
        #     logger.info("%s/%s#%d: no need to update", issue.owner, issue.name, issue.number)
        #     continue
        update_issue_content(issue)
        logger.info(
            "%s/%s#%d is done (%d of %d open issues)",
            issue.owner,
            issue.name,
            issue.number,
            i,
            len(all_issues),
        )


def get_dataset_for_repo(
    owner: str,
    name: str,
    since: datetime,
    github_login: str = None,
    init_db: bool = False,
):
    """
    Update the Dataset collection with latest resolved and open issues for a single repo.
    """
    print("get_dataset_for_repo begain---")
    if init_db:
        mongoengine.disconnect_all()
        mongoengine.connect(
            CONFIG["mongodb"]["db"],
            host=CONFIG["mongodb"]["url"],
            tz_aware=True,
            uuidRepresentation="standard",
        )

    if update_in_progress(owner, name, DatasetBuildLog) or update_in_progress(
        owner, name, GitHubFetchLog
    ):
        logger.info("%s/%s is already being updated, skipping", owner, name)
        return

    log = DatasetBuildLog(
        owner=owner,
        name=name,
        pid=os.getpid(),
        user_github_login=github_login,
        update_begin=datetime.utcnow(),
    )
    log.save()

    repo_query = Q(owner=owner) & Q(name=name)
    all_issues = list(
        RepoIssue.objects(repo_query)
    )
    update_dataset_with_issues(all_issues)

    # log.updated_open_issues = len(open_issues)
    # log.updated_resolved_issues = len(resolved_issues)
    log.update_end = datetime.utcnow()
    log.save()


def get_dataset_all(since: datetime, n_process: int = None):
    """Update the Dataset collection with latest resolved and open issues.

    Args:
        since (datetime, optional): Only consider issues updated after this time.
              Defaults to None, which means to consider all issues.
        n_process (int, optional): Number of processes to use. Defaults to None
    """
    if n_process is None:
        # repos = [(r.owner, r.name) for r in Repo.objects()]
        repos = [("microsoft", "vscode")]
        for owner, name in repos:
            get_dataset_for_repo(owner, name, since)
            print("n_process is None")
    else:
        params = [(r.owner, r.name, since, None, True) for r in Repo.objects()]
        print(params)
        with mp.Pool(n_process) as p:
            p.starmap(get_dataset_for_repo, params)
            print("n_process is not None")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=str, default="2008.01.01")
    # parser.add_argument("--nprocess", type=int, default=mp.cpu_count())
    parser.add_argument("--nprocess", type=int, default=None)
    args = parser.parse_args()
    since, nprocess = parse_date(args.since), args.nprocess

    logger.info("Start!")

    mongoengine.connect(
        CONFIG["mongodb"]["db"],
        host=CONFIG["mongodb"]["url"],
        tz_aware=True,
        uuidRepresentation="standard",
    )

    get_dataset_all(since, nprocess)

    logger.info("Finish!")
