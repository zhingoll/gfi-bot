import os
import re
import logging
import argparse
import mongoengine
import numpy as np
import multiprocessing as mp

from typing import List, Dict, Set, Any, Optional
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pymongo.errors import DocumentTooLarge
from gfibot import CONFIG, TOKENS
from gfibot.check_tokens import check_tokens
from gfibot.collections import *
from gfibot.data.mygraphql import UserFetcher
from gfibot.data.rest import RepoFetcher, logger as rest_logger
from github import RateLimitExceededException, UnknownObjectException
import time

logger = logging.getLogger(__name__)


# def _count_by_month(dates: List[datetime]) -> List[Repo.MonthCount]:
#     counts = Counter(map(lambda d: (d.year, d.month), dates))
#     return sorted(
#         [
#             Repo.MonthCount(
#                 month=datetime(year=y, month=m, day=1, tzinfo=timezone.utc), count=c
#             )
#             for (y, m), c in counts.items()
#         ],
#         key=lambda k: k["month"],
#     )


# def _match_issue_numbers(text: str) -> List[int]:
#     """
#     Match close issue text in a pull request, as documented in:
#     https://docs.github.com/en/issues/tracking-your-work-with-issues/linking-a-pull-request-to-an-issue
#     """
#     numbers = []
#     regex = r"(close[sd]?|fix(es|ed)?|resolve[sd]?) \#(\d+)"
#     for _, _, number in re.findall(regex, text.lower()):
#         numbers.append(int(number))
#     return numbers


def _update_repo_info(fetcher: RepoFetcher) -> Repo:
    logger.info("Updating repo: %s/%s", fetcher.owner, fetcher.name)
    repo = Repo.objects(owner=fetcher.owner, name=fetcher.name)
    if repo.count() == 0:
        logger.info("Repo not found in database, creating...")
        repo = Repo(
            # created_at=datetime.now(timezone.utc),  # 这里应该修改为真实的仓库创建时间？
            created_at=fetcher.created_at,
            updated_at=None,
            owner=fetcher.owner,
            name=fetcher.name,
        )
    else:
        repo = repo.first()

    for k, v in fetcher.get_stats().items():
        if k == "languages":
            v = [Repo.LanguageCount(language=k2, count=v2) for k2, v2 in v.items()]
        setattr(repo, k, v)
    logger.info("Repo stats updated, rate = %s", fetcher.rate)
    return repo


def _update_stars(fetcher: RepoFetcher, since: datetime) -> List[Dict[str, Any]]:
    stars = fetcher.get_stars(since)
    logger.info("%d stars updated, rate = %s", len(stars), fetcher.rate)
    for star in stars:
        RepoStar.objects(
            owner=fetcher.owner, name=fetcher.name, user=star["user"]
        ).upsert_one(**star)  # 将字典扩展为关键字参数 比如：upsert_one(user="username123", starred_at="2022-01-01T12:00:00Z")
    return stars


def _update_commits(fetcher: RepoFetcher, since: datetime) -> List[Dict[str, Any]]:
    commits = fetcher.get_commits(since)
    logger.info(
        "%d commits updated, rate = %s",
        len(commits),
        fetcher.rate,
    )
    for commit in commits:
        RepoCommit.objects(
            owner=fetcher.owner, name=fetcher.name, sha=commit["sha"]
        ).upsert_one(**commit)
    return commits


def _update_issues(fetcher: RepoFetcher, since: datetime) -> List[Dict[str, Any]]:
    # 正常拿数据时使用
    print("get issues begin!!!")
    issues = fetcher.get_issues(since)
    print("issues get done!!!")
    # issues = fetcher.get_issue(162900)
    logger.info(
        "%d issues updated, rate = %s",
        len(issues),
        fetcher.rate,
    )
    for issue in issues:  # repoissue的构建，需要加入events属性
        RepoIssue.objects(
            owner=fetcher.owner, name=fetcher.name, number=issue["number"]
        ).upsert_one(**issue)

    logger.info("There are %d issues in repo %s since %s", len(issues),fetcher.name ,since)
    return issues

    #   测试时使用
    # query = Q(name=fetcher.name, owner=fetcher.owner)
    # repo_all_issues = RepoIssue.objects(query)
    # logger.info("There are %d issues in repo %s since %s", len(repo_all_issues),fetcher.name ,since)
    return repo_all_issues



# def _update_repo_stats(repo: Repo):
#     owner, name = repo.owner, repo.name
#     all_issues: List[RepoIssue] = list(RepoIssue.objects(owner=owner, name=name))
#
#     # Median issue close time
#     closed_t = [
#         (i.closed_at - i.created_at).total_seconds()
#         for i in all_issues
#         if i.state == "closed" and not i.is_pull and i.closed_at is not None
#     ]
#     repo.median_issue_close_time = np.median(closed_t) if len(closed_t) > 0 else None
#
#     # Monthly data
#     repo.monthly_stars = _count_by_month(
#         RepoStar.objects(owner=owner, name=name).scalar("starred_at")
#     )
#     repo.monthly_commits = _count_by_month(
#         RepoCommit.objects(owner=owner, name=name).scalar("committed_at")
#     )
#     repo.monthly_issues = _count_by_month(
#         [i.created_at for i in all_issues if not i.is_pull]
#     )
#     repo.monthly_pulls = _count_by_month(
#         [i.created_at for i in all_issues if i.is_pull]
#     )


# def _locate_resolved_issues(  # 这个方法需要大修！！！
#     fetcher: RepoFetcher, since: datetime
# ) -> List[Dict[str, Any]]:
#     all_issues: Dict[int, RepoIssue] = {
#         i.number: i
#         for i in RepoIssue.objects(
#             owner=fetcher.owner, name=fetcher.name, is_pull=False, closed_at__gte=since
#         )
#     }
#     # all_commits: List[RepoCommit] = sorted(
#     #     RepoCommit.objects(owner=fetcher.owner, name=fetcher.name),
#     #     key=lambda c: c.authored_at,
#     # )
#     closed_nums = set(map(lambda i: i.number, all_issues.values()))  # 已经关闭了的issue number集合
#     logger.info("%d newly closed issues since %s", len(all_issues), since)
#
#     resolved = defaultdict(  # 允许为字典提供一个默认的数据类型（作为新字典的值），当尝试访问字典中不存在的键时，它会自动为该键生成一个默认值。
#         lambda: {
#             "owner": fetcher.owner,
#             "name": fetcher.name,
#             "number": None,
#             "created_at": None,
#             "resolved_at": None,
#             "resolver": None,
#             "resolved_in": None,
#             "resolver_commit_num": None,
#             "events": [],
#             "issue_opener": None,
#         }
#     )
#
#     author2commits = defaultdict(list)  # 键为提交的作者，值为一个列表，存储该作者的所有提交。
#     for c in all_commits:
#         author2commits[c.author].append(c)
#     for c in all_commits:
#         if c.author is None:
#             continue
#         commits_before = set()
#         for c2 in author2commits[c.author]:
#             if c2.authored_at < c.authored_at:
#                 commits_before.add(c2.sha)
#         for num in _match_issue_numbers(c.message):  # 这里匹配有问题，需要修改！！！因为这里只从commit中提取信息，太单一。
#             if num not in closed_nums:
#                 continue
#             logger.debug(
#                 "Issue #%d resolved in %s by %s (%d prior commits)",
#                 num,
#                 c.sha,
#                 c.author,
#                 len(commits_before),
#             )
#             resolved[num]["number"] = num
#             resolved[num]["resolver"] = c.author
#             resolved[num]["resolved_in"] = c.sha
#             resolved[num]["resolver_commit_num"] = len(commits_before)
#     logger.info("%d issues found to be resolved by commits", len(resolved))
#
#
#
#     for issue in all_issues.values():
#         t1 = issue.closed_at - timedelta(minutes=1)
#         t2 = issue.closed_at + timedelta(minutes=1)
#         prs: List[RepoIssue] = list(
#             RepoIssue.objects(
#                 owner=fetcher.owner,
#                 name=fetcher.name,
#                 is_pull=True,
#                 merged_at__gt=t1,
#                 merged_at__lt=t2,
#             )
#         )
#         if len(prs) > 0:
#             logger.debug(
#                 "Candidate PRs %s for issue %d, rate = %s",
#                 [pr.number for pr in prs],
#                 issue.number,
#                 fetcher.rate,
#             )
#         for pr in prs:
#             pr_details = fetcher.get_pull_detail(pr.number)
#             text = [pr.title, pr.body, *pr_details["comments"]]
#             text = "\n".join([t for t in text if t is not None])
#             commits_before = set()
#             for c in author2commits[pr.user]:
#                 if (
#                     c.authored_at < pr.merged_at - timedelta(days=1)
#                     and c.sha not in pr_details["commits"]
#                 ):
#                     commits_before.add(c.sha)
#             if issue.number in _match_issue_numbers(text):
#                 logger.debug(
#                     "Issue #%d resolved in #%d by %s (%d prior commits)",
#                     issue.number,
#                     pr.number,
#                     pr.user,
#                     len(commits_before),
#                 )
#                 resolved[issue.number]["number"] = issue.number
#                 resolved[issue.number]["resolver"] = pr.user
#                 resolved[issue.number]["resolved_in"] = pr.number
#                 resolved[issue.number]["resolver_commit_num"] = len(commits_before)
#     logger.info("%d issues found to be resolved by commits/PRs", len(resolved))
#
#     for num in resolved.keys():
#         resolved[num]["created_at"] = all_issues[num].created_at
#         resolved[num]["resolved_at"] = all_issues[num].closed_at
#     return list(resolved.values())


# def _update_resolved_issues(
#     fetcher: RepoFetcher, since: datetime
# ) -> List[Dict[str, Any]]:
#     """Fetch data for issues that will be used for RecGFI training."""
#     resolved_issues = _locate_resolved_issues(fetcher, since)
#     for resolved_issue in resolved_issues:
#         logger.debug(
#             "Fetching details for resolved issue #%d, rate = %s",
#             resolved_issue["number"],
#             fetcher.rate,
#         )
#         for event in fetcher.get_issue_detail(resolved_issue["number"])["events"]:
#             resolved_issue["events"].append(IssueEvent(**event))
#     for resolved_issue in resolved_issues:
#         ResolvedIssue.objects(
#             owner=fetcher.owner, name=fetcher.name, number=resolved_issue["number"]
#         ).upsert_one(**resolved_issue)
#     return resolved_issues



# def save_resolved_issue(data, issue_events, closed_issues, part=None):
#     try:
#         data_copy = data.copy()
#         if part is not None:
#             data_copy['part'] = part  # 加入部分标识
#         resolved_issue = ResolvedIssue(**data_copy, events=issue_events)
#         resolved_issue.save()
#         closed_issues.append(resolved_issue)
#     except DocumentTooLarge as e:
#         logger.error(f"Document too large for issue {data['number']}: {str(e)}")
#         mid_index = len(issue_events) // 2
#         print(f"Issue {data['number']} is too large, starting recursive split with {len(issue_events)} events.")
#         save_resolved_issue(data, issue_events[:mid_index], closed_issues, part="first_half")
#         save_resolved_issue(data, issue_events[mid_index:], closed_issues, part="second_half")


# def _update_closed_issues(fetcher: RepoFetcher, nums: List[int], since: datetime):
#     query = Q(name=fetcher.name, owner=fetcher.owner)
#     repo_closed_issues = RepoIssue.objects(query & Q(is_pull=False, state="closed", number__in=nums))
#     logger.info("%d closed issues updated since %s", repo_closed_issues.count(), since)
#
#     closed_issues = []
#     for issue in list(repo_closed_issues):
#         if ResolvedIssue.objects(query & Q(number=issue.number)).count() > 0:
#             continue
#
#         try:
#             issues_detail = fetcher.get_issue_detail(issue.number)
#             issue_events = [IssueEvent(**e) for e in issues_detail["events"]]
#             issue_data = {
#                 "name": fetcher.name,
#                 "owner": fetcher.owner,
#                 "number": issue.number,
#                 "created_at": issue.created_at,
#                 "resolved_at": issue.closed_at,
#                 "resolver": issues_detail["resolver"],
#                 "issue_opener": issue.user
#             }
#             save_resolved_issue(issue_data, issue_events, closed_issues)
#         except ValueError as e:
#             logger.error(f"Failed to fetch issue {issue.number}: {str(e)}")
#             with open('failed_issues.txt', 'a') as file:
#                 file.write(f"Issue Number: {issue.number}\n")
#
#         logger.debug("Fetching details for closed issue #%d, rate = %s", issue.number, fetcher.rate)
#
#     return closed_issues


def _update_closed_issues(fetcher: RepoFetcher, nums: List[int], since: datetime):
    """Fetch data for all closed issues"""
    query = Q(name=fetcher.name, owner=fetcher.owner)
    repo_closed_issues = RepoIssue.objects(
        query & Q(is_pull=False, state="closed", number__in=nums)
    )
    logger.info("%d closed issues updated since %s", repo_closed_issues.count(), since)

    # 获取closed issues
    closed_issues = []
    for issue in list(repo_closed_issues):
        existing = ResolvedIssue.objects(query & Q(number=issue.number))
        if existing.count() > 0:
            continue
        else:
            # 获取events、resolver完善ResolvedIssue
            try:
                issues_detail = fetcher.get_issue_detail(issue.number)
            except ValueError as e:
                # 处理未找到issue的情况，记录错误并继续处理下一个issue
                logger.error(f"Failed to fetch issue {issue.number}: {str(e)}")
                # Write failed issues to a file
                with open('failed_issues.txt', 'a') as file:  # Append mode
                    file.write(f"Issue Number: {issue.number}\n")
                continue

            issue_resolver = issues_detail["resolver"]
            issue_events = [IssueEvent(**e) for e in issues_detail["events"]]
            closed_issue = ResolvedIssue(
                name=fetcher.name,
                owner=fetcher.owner,
                number=issue.number,
                created_at=issue.created_at,
                resolved_at=issue.closed_at,
                resolver = issue_resolver,
                events= issue_events,
                issue_opener = issue.user,
            )
        try:
            closed_issue.save()
            closed_issues.append(closed_issue)
        except DocumentTooLarge as e:
            logger.error(f"Document too large for issue {issue.number}: {str(e)}")
        logger.debug(
            "Fetching details for closed issue #%d, rate = %s",
            issue.number,
            fetcher.rate,
        )
        closed_issue.save()
        closed_issues.append(closed_issue)
    return closed_issues

    # 测试时使用
    # query = Q(name=fetcher.name, owner=fetcher.owner)
    # repo_all_resolvedissues = ResolvedIssue.objects(query)
    # logger.info("There are %d resolved issues in repo %s since %s", repo_all_resolvedissues.count(),fetcher.name ,since)
    return repo_all_resolvedissues



def _update_open_issues(fetcher: RepoFetcher, nums: List[int], since: datetime):
    """Fetch data for all new open issues"""
    query = Q(name=fetcher.name, owner=fetcher.owner)
    repo_open_issues = RepoIssue.objects(
        query & Q(is_pull=False, state="open", number__in=nums)
    )
    logger.info("%d open issues updated since %s", repo_open_issues.count(), since)

    open_issues = []
    for issue in list(repo_open_issues):
        existing = OpenIssue.objects(query & Q(number=issue.number))
        if existing.count() > 0:
            open_issue = existing.first()
            open_issue.updated_at = datetime.utcnow()  # 系统更新时间，不是issue最后关闭时间
        else:
            try:
                issues_detail = fetcher.get_issue_detail(issue.number)
            except ValueError as e:
                # 处理未找到issue的情况，记录错误并继续处理下一个issue
                logger.error(f"Failed to fetch issue {issue.number}: {str(e)}")
                # Write failed issues to a file
                with open('failed_issues.txt', 'a') as file:  # Append mode
                    file.write(f"Issue Number: {issue.number}\n")
                continue
            issue_events = [IssueEvent(**e) for e in issues_detail["events"]]

            open_issue = OpenIssue(
                name=fetcher.name,
                owner=fetcher.owner,
                number=issue.number,
                created_at=issue.created_at,
                updated_at=datetime.utcnow(),
                events=issue_events,
                issue_opener=issue.user,
            )
            try:
                open_issue.save()

            except DocumentTooLarge as e:
                logger.error(f"Document too large for issue {issue.number}: {str(e)}")
                open_issue.events = []
                open_issue.save()  # 保存没有events的主文档

                # 分批保存events
                event_chunks = [issue_events[i:i + 100] for i in range(0, len(issue_events), 100)]
                for i, chunk in enumerate(event_chunks):
                    issue_supplement_event = IssueEventSupplement(
                        name=fetcher.name,
                        owner=fetcher.owner,
                        number=issue.number,
                        events=chunk,
                        part=i
                    )
                    issue_supplement_event.save()

            open_issues.append(open_issue)

        logger.debug(
            "Fetching details for open issue #%d, rate = %s",
            issue.number,
            fetcher.rate,
        )


    # Delete issues that are closed now
    closed_issue_nums = list(
        RepoIssue.objects(query & Q(is_pull=False, state="closed")).scalar("number")
    )
    OpenIssue.objects(query & Q(number__in=closed_issue_nums)).delete()

    return open_issues

    # 测试时使用
    # query = Q(name=fetcher.name, owner=fetcher.owner)
    # repo_all_openissues = OpenIssue.objects(query)
    # logger.info("There are %d open issues in repo %s since %s", repo_all_openissues.count(),fetcher.name ,since)
    # return repo_all_openissues

def _update_closed_prs(fetcher: RepoFetcher, nums: List[int], since: datetime):
    """Fetch data for all closed issues"""
    query = Q(name=fetcher.name, owner=fetcher.owner)
    repo_closed_prs = RepoIssue.objects(
        query & Q(is_pull=True, state="closed", number__in=nums)
    )
    logger.info("%d closed prs updated since %s", repo_closed_prs.count(), since)

    # 获取closed prs
    closed_prs = []
    for pr in list(repo_closed_prs):
        existing = ClosedPr.objects(query & Q(number=pr.number))
        if existing.count() > 0:
            continue
        else:
            # 获取reviews、comment完善PR
            retries = 3
            while retries > 0:
                try:
                    pr_detail = fetcher.get_pr_detail(pr.number)
                    break
                except ValueError as e:
                    # 处理未找到issue的情况，记录错误并继续处理下一个issue
                    logger.error(f"Failed to fetch closed pr {pr.number}: {str(e)}")
                    # Write failed issues to a file
                    with open('failed_issues.txt', 'a') as file:  # Append mode
                        file.write(f"Closed PR Number: {pr.number}\n")
                    continue
                except RateLimitExceededException as ex:
                    logging.info(f"{type(ex)}: {ex}")
                    logging.info(f"Rate limit reached, Preparing to switch tokens...")
                    time.sleep(3)
                    fetcher.rotate_token()  # Rotate token on the last retry failure
                except UnknownObjectException as ex:
                    logging.error(f"{type(ex)}: {ex}")
                    break
                except Exception as ex:
                    logging.error(f"{type(ex)}: {ex}")
                    time.sleep(5)
                retries -= 1

            reviewer_events = [PREvent(**e) for e in pr_detail["reviewer_events"]]
            normal_commenter_events = [PREvent(**e) for e in pr_detail["normal_commenter_events"]]
            label_events = [PREvent(**e) for e in pr_detail["label_events"]]
            closed_pr = ClosedPr(
                name=fetcher.name,
                owner=fetcher.owner,
                number=pr.number,
                created_at=pr.created_at,
                closed_at=pr.closed_at,
                reviewer_events=reviewer_events,
                normal_commenter_events=normal_commenter_events,
                label_events=label_events,
                pr_opener=pr.user,
            )
        logger.debug(
            "Fetching details for closed_prs #%d, rate = %s",
            pr.number,
            fetcher.rate,
        )
        closed_pr.save()
        closed_prs.append(closed_pr)

    return closed_prs
    # 测试时使用
    # query = Q(name=fetcher.name, owner=fetcher.owner)
    # repo_all_ClosedPr = ClosedPr.objects(query)
    # logger.info("There are %d Closed Pr in repo %s since %s", repo_all_ClosedPr.count(),fetcher.name ,since)
    # return repo_all_ClosedPr

def _update_open_prs(fetcher: RepoFetcher, nums: List[int], since: datetime):
    """Fetch data for all new open issues"""
    query = Q(name=fetcher.name, owner=fetcher.owner)
    repo_open_prs = RepoIssue.objects(
        query & Q(is_pull=True, state="open", number__in=nums)
    )
    logger.info("%d open prs updated since %s", repo_open_prs.count(), since)

    open_prs = []
    for pr in list(repo_open_prs):
        existing = OpenPr.objects(query & Q(number=pr.number))
        if existing.count() > 0:
            open_pr = existing.first()
            open_pr.updated_at = datetime.utcnow()  # 系统更新时间，不是issue最后关闭时间
        else:
            # # 获取reviews、comment完善PR
            # try:
            #     pr_detail = fetcher.get_pr_detail(pr.number)
            # except ValueError as e:
            #     # 处理未找到issue的情况，记录错误并继续处理下一个issue
            #     logger.error(f"Failed to fetch pr {pr.number}: {str(e)}")
            #     # Write failed issues to a file
            #     with open('failed_issues.txt', 'a') as file:  # Append mode
            #         file.write(f"PR Number: {pr.number}\n")
            #     continue

            # 获取reviews、comment完善PR
            retries = 3
            while retries > 0:
                try:
                    pr_detail = fetcher.get_pr_detail(pr.number)
                    break
                except ValueError as e:
                    # 处理未找到issue的情况，记录错误并继续处理下一个issue
                    logger.error(f"Failed to fetch open pr {pr.number}: {str(e)}")
                    # Write failed issues to a file
                    with open('failed_issues.txt', 'a') as file:  # Append mode
                        file.write(f"Open PR Number: {pr.number}\n")
                    continue
                except RateLimitExceededException as ex:
                    logging.info(f"{type(ex)}: {ex}")
                    logging.info(f"Rate limit reached, Preparing to switch tokens...")
                    time.sleep(3)
                    fetcher.rotate_token()  # Rotate token on the last retry failure
                except UnknownObjectException as ex:
                    logging.error(f"{type(ex)}: {ex}")
                    break
                except Exception as ex:
                    logging.error(f"{type(ex)}: {ex}")
                    time.sleep(5)
                retries -= 1

            reviewer_events = [PREvent(**e) for e in pr_detail["reviewer_events"]]
            normal_commenter_events = [PREvent(**e) for e in pr_detail["normal_commenter_events"]]
            label_events = [PREvent(**e) for e in pr_detail["label_events"]]
            open_pr = OpenPr(
                name=fetcher.name,
                owner=fetcher.owner,
                number=pr.number,
                created_at=pr.created_at,
                updated_at=datetime.utcnow(),
                reviewer_events=reviewer_events,
                normal_commenter_events=normal_commenter_events,
                label_events=label_events,
                pr_opener=pr.user,
            )
        logger.debug(
            "Fetching details for open_pr #%d, rate = %s",
            pr.number,
            fetcher.rate,
        )
        open_pr.save()
        open_prs.append(open_pr)
    # Delete issues that are closed now
    closed_pr_nums = list(
        RepoIssue.objects(query & Q(is_pull=True, state="closed")).scalar("number")
    )
    OpenPr.objects(query & Q(number__in=closed_pr_nums)).delete()

    return open_prs

    # 测试时使用
    # query = Q(name=fetcher.name, owner=fetcher.owner)
    # repo_all_OpenPr = OpenPr.objects(query)
    # logger.info("There are %d Open Pr in repo %s since %s", repo_all_OpenPr.count(),fetcher.name ,since)
    # return repo_all_OpenPr

def _find_users(
    owner: str,
    name: str,
    issues: list,
    open_issues: list,
    resolved_issues: list,
    open_prs: list,
    closed_prs: list,
) -> Set[str]:
    all_users = set([owner])
    # 获取所有issue的opener
    for issue in issues:
        all_users.add(issue["user"])
    # 获取所有issue的eventer
    for open in open_issues:
        for event in open["events"]:
            all_users.add(event["actor"])
    for resolved in resolved_issues:
        all_users.update(resolved["resolver"])
        for event in resolved["events"]:
            all_users.add(event["actor"])
    for open in open_prs:
        for event in open["reviewer_events"]:
            all_users.add(event["actor"])
        for event in open["normal_commenter_events"]:
            all_users.add(event["actor"])
    for closed in closed_prs:
        for event in closed["reviewer_events"]:
            all_users.add(event["actor"])
        for event in closed["normal_commenter_events"]:
            all_users.add(event["actor"])

    if None in all_users:
        all_users.remove(None)
    logger.info("%d users associated with %s/%s", len(all_users), owner, name)
    return all_users

def filter_user(all_users):
    # 获取数据库中所有用户的 login 字段
    all_stored_users = User.objects().distinct('login')  # 使用 .distinct() 来直接获取唯一的 login 列表

    # 使用集合操作从 all_users 中删除已存在的 login
    remaining_users = set(all_users) - set(all_stored_users)

    logger.info("There are %d non-existent users", len(remaining_users))
    return remaining_users


def _update_user_issues(user: User, res: Dict[str, Any]) -> None:
    """Update issues for a user."""
    user.issues = [
        User.Issue(
            owner=issue["repository"]["nameWithOwner"].split("/")[0],
            name=issue["repository"]["nameWithOwner"].split("/")[1],
            repo_stars=issue["repository"]["stargazerCount"],
            state=issue["state"],
            number=issue["number"],
            created_at=issue["createdAt"],
        )
        for issue in res["nodes"]
    ]


def _update_user_pulls(user: User, res: Dict[str, Any]) -> None:
    """Update pull request contributions for a user."""
    user.pulls = [
        User.Pull(
            owner=pr["pullRequest"]["repository"]["nameWithOwner"].split("/")[0],
            name=pr["pullRequest"]["repository"]["nameWithOwner"].split("/")[1],
            repo_stars=pr["pullRequest"]["repository"]["stargazerCount"],
            state=pr["pullRequest"]["state"],
            created_at=pr["pullRequest"]["createdAt"],
            number=pr["pullRequest"]["number"],
        )
        for pr in res["nodes"]
    ]


def _update_user_commits(user: User, res: Dict[str, Any]) -> None:
    """Update commits for a user."""
    user.commits = []
    for commit_contrib in res:
        owner = commit_contrib["repository"]["nameWithOwner"].split("/")[0]
        name = commit_contrib["repository"]["nameWithOwner"].split("/")[1]
        repo_stars = commit_contrib["repository"]["stargazerCount"]

        for contrib in commit_contrib["contributions"]["nodes"]:
            user.commit_contributions.append(
                User.CommitContribution(
                    owner=owner,
                    name=name,
                    repo_stars=repo_stars,
                    commit_count=contrib["commitCount"],
                    created_at=contrib["occurredAt"],
                )
            )


def _update_user_reviews(user: User, res: Dict[str, Any]) -> None:
    """Update reviews for a user."""
    user.pull_reviews = [
        User.Review(
            owner=review["repository"]["nameWithOwner"].split("/")[0],
            name=review["repository"]["nameWithOwner"].split("/")[1],
            repo_stars=review["repository"]["stargazerCount"],
            created_at=review["pullRequestReview"]["createdAt"],
            state=review["pullRequestReview"]["state"],
            number=review["pullRequestReview"]["pullRequest"]["number"],
        )
        for review in res["nodes"]
    ]


def _update_user_meta(user: User, res: Dict[str, Any]) -> None:
    """Update meta data for a user."""
    user.name = res["name"]


def _update_user_query(rate_state: dict, res: Dict[str, Any]) -> None:
    rate_state["remaining"] = res["rateLimit"]["remaining"]
    rate_state["resetAt"] = res["rateLimit"]["resetAt"]
    rate_state["cost"] += res["rateLimit"]["cost"]


def update_user(tokens: List[str], login: str) -> int:
    """Fetch data for a user"""
    # does the user exist?
    user = User.objects(login=login).first()
    time_now = datetime.utcnow()
    if user is None:
        user = User(login=login, _created_at=time_now)
        since = datetime(2008, 1, 1)  # GitHub was launched in 2008
    else:
        since = user._updated_at

    user._updated_at = time_now

    # Fix 'rate limit exceed' in CI environment
    _is_ci = os.environ.get("CI", "")
    if _is_ci:
        logger.info("Running in CI environment, overriding 'since' date")
        since = time_now - timedelta(days=7)

    rate_state = {"cost": 0}

    fetcher = UserFetcher(
        tokens=tokens,
        login=login,
        since=since,
        callbacks={
            "query": lambda res: _update_user_query(rate_state, res),
            "user": lambda res: _update_user_meta(user, res),
            "issues": lambda res: _update_user_issues(user, res),
            "pullRequestContributions": lambda res: _update_user_pulls(user, res),
            "commitContributionsByRepository": lambda res: _update_user_commits(
                user, res
            ),
            "pullRequestReviewContributions": lambda res: _update_user_reviews(
                user, res
            ),
        },
    )
    try:
        fetcher.fetch()  # 获取数据
        user.save()  # 存入数据库
        logger.info(
            "User %s updated from %s to %s, ratelimit cost=%d remaining=%d",
            login,
            since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            user._updated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            rate_state["cost"],
            rate_state["remaining"],
        )
    except Exception as e:
        logger.error("Failed to update user %s", login)
        logger.exception(e)

    return rate_state["cost"]


# def update_gfi_repo_add_query(owner: str, name: str) -> None:
#     """TODO: Remove this function after we have a better logging system"""
#     GfiQueries.objects(Q(owner=owner) & Q(name=name)).update_one(
#         set__is_pending=False,
#         set__is_finished=True,
#         set__is_updating=False,
#         set___finished_at=datetime.now(timezone.utc),
#     )


def update_repo(
    tokens: List[str], owner: str, name: str, user_github_login: Optional[str] = None
) -> None:
    """Update all information of a repository for RecGFI training

    Args:
        token (str): A GitHub access token
        owner (str): repository owner
        name (str): repository name
        user_github_login (Optional[str], optional):
            If this function is called from backend, indicate which user intiated this update.
            Defaults to None.
    """
    if update_in_progress(owner, name, GitHubFetchLog):
        logger.info("%s/%s is already being updated, skipping", owner, name)
        return

    log = GitHubFetchLog(
        owner=owner,
        name=name,
        pid=os.getpid(),
        update_begin=datetime.now(timezone.utc),
        user_github_login=user_github_login,
    )
    log.save()

    fetcher = RepoFetcher(tokens, owner, name)

    logger.info("Fetching repo %s/%s", owner, name)
    repo = _update_repo_info(fetcher)

    if repo.updated_at is None:
        since = repo.repo_created_at
    else:
        since = repo.updated_at
    repo.updated_at = datetime.now(timezone.utc)

    logger.info("Update stars, commits, and issues since %s", since)
    # stars = _update_stars(fetcher, since)   # 暂时不考虑star和commit相关信息
    # commits = _update_commits(fetcher, since)
    all_issues = _update_issues(fetcher, since)

    # log.updated_stars = len(stars)
    log.updated_issues = len(all_issues)
    # log.updated_commits = len(commits)
    log.rate = fetcher.rate_consumed
    log.rate_repo_stat = fetcher.rate_consumed
    log.save()


    # 找出open_issues 和 closed_issues
    closed_issue_nums = [
        i["number"] for i in all_issues if i["state"] == "closed" and not i["is_pull"]
    ]
    resolved_issues = _update_closed_issues(fetcher, closed_issue_nums,since)
    log.updated_resolved_issues = len(resolved_issues)
    log.rate = fetcher.rate_consumed
    log.rate_resolved_issue = fetcher.rate_consumed - log.rate_repo_stat
    log.save()

    open_issue_nums = [
        i["number"] for i in all_issues if i["state"] == "open" and not i["is_pull"]
    ]
    open_issues = _update_open_issues(fetcher, open_issue_nums, since)
    log.updated_open_issues = len(open_issues)
    log.rate = fetcher.rate_consumed
    log.rate_open_issue = (
        fetcher.rate_consumed - log.rate_repo_stat - log.rate_resolved_issue
    )
    log.save()

    # 找出closed_pr
    closed_pr_nums = [
        i["number"] for i in all_issues if i["state"] == "closed" and i["is_pull"]
    ]
    closed_prs = _update_closed_prs(fetcher, closed_pr_nums, since)
    log.updated_closed_prs = len(closed_prs)
    log.rate = fetcher.rate_consumed
    log.rate_closed_pr = (
        fetcher.rate_consumed - log.rate_repo_stat - log.rate_resolved_issue - log.rate_open_issue
    )
    log.save()

    # 找出open_pr
    open_pr_nums = [
        i["number"] for i in all_issues if i["state"] == "open" and i["is_pull"]
    ]
    open_prs = _update_open_prs(fetcher, open_pr_nums, since)
    log.updated_open_prs = len(open_prs)
    log.rate = fetcher.rate_consumed
    log.rate_open_pr = (
        fetcher.rate_consumed - log.rate_repo_stat - log.rate_resolved_issue - log.rate_open_issue - log.rate_closed_pr
    )
    log.save()

    # update_gfi_repo_add_query(owner, name)
    all_users = _find_users(owner, name, all_issues,open_issues, resolved_issues, open_prs, closed_prs)
    print(len(all_users))

    all_users = filter_user(all_users)

    log.rate_user = 0
    for user in all_users:
        if user is None or type(user) != str:
            continue
        log.rate_user += update_user(tokens, user)  # 这里的user不能是组织
    log.updated_users = len(all_users)
    log.rate = log.rate + log.rate_user
    log.update_end = datetime.now(timezone.utc)
    log.save()

    _update_repo_stats(repo)
    repo.save()
    logger.info("Finished updating for %s/%s since %s", owner, name, since)


def update_under_tokens(tokens: List[str], repos: List[str]) -> None:
    # Reconnect in a new process
    mongoengine.connect(
        CONFIG["mongodb"]["db"],
        host=CONFIG["mongodb"]["url"],
        tz_aware=True,
        uuidRepresentation="standard",
    )

    logging.info("token = %s, repos = %s", str([token[0:6] + ' ' for token in tokens]), repos)
    for repo in repos:
        owner, name = repo.split("/")
        update_repo(tokens, owner, name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--nprocess", type=int, default=mp.cpu_count())
    print("cpu_count() -> ",mp.cpu_count())
    parser.add_argument("--repos", type=str, default="microsoft/vscode")
    args = parser.parse_args()
    if args.debug:
        logger.setLevel(logging.DEBUG)
        rest_logger.setLevel(logging.DEBUG)
    if args.repos == "":
        repos = CONFIG["gfibot"]["projects"]
    else:
        repos = args.repos.split(",")
        print(repos)

    # run check_tokens before update
    failed_tokens = check_tokens(TOKENS)
    print(failed_tokens)
    valid_tokens = list(set(TOKENS) - failed_tokens)
    print(valid_tokens)
    logger.info("Data update started at {}".format(datetime.now()))

    params = defaultdict(list)
    for i, project in enumerate(repos):
        primary_token = valid_tokens[i % len(valid_tokens)]
        secondary_token = valid_tokens[(i + 1) % len(valid_tokens)]
        params[(primary_token, secondary_token)].append(project)
        # params[valid_tokens[i % len(valid_tokens)]].append(project)
    with mp.Pool(min(args.nprocess, len(valid_tokens))) as pool:
        pool.starmap(update_under_tokens, params.items())

    logger.info("Data update finished at {}".format(datetime.now()))


if __name__ == "__main__":
    main()
