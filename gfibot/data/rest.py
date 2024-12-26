import time
import logging

from typing import *
from datetime import datetime, timezone
from calendar import monthrange
from dateutil.parser import parse as parse_date
from github import Github
from github import RateLimitExceededException, UnknownObjectException
from github.GithubObject import NotSet
from gfibot.collections import *
from collections import defaultdict

T = TypeVar("T")
logger = logging.getLogger(__name__)


def get_page_num(per_page: int, total_count: int) -> int:
    """Calculate total number of pages given page size and total number of items"""
    assert per_page > 0 and total_count >= 0
    if total_count % per_page == 0:
        return total_count // per_page
    return total_count // per_page + 1


def get_month_interval(date: datetime) -> Tuple[datetime, datetime]:
    if date.tzinfo is None:
        logger.warning("date is not timezone aware: {}".format(date))
        date = date.replace(tzinfo=timezone.utc)
    date = date.astimezone(timezone.utc)
    since = date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    until = date.replace(
        day=monthrange(date.year, date.month)[1],
        hour=23,
        minute=59,
        second=59,
        microsecond=999999,
    )
    return since, until


# def request_github(
#     gh: Github, gh_func: Callable[..., T], params: Tuple = (), default: Any = None
# ) -> Optional[T]:
#     """
#     This is a wrapper to ensure that any rate-consuming interactions with GitHub
#       have proper exception handling.
#     """
#     for _ in range(0, 3):  # Max retry 3 times
#         try:
#             data = gh_func(*params)
#             return data
#         except RateLimitExceededException as ex:
#             logger.info("{}: {}".format(type(ex), ex))
#             sleep_time = gh.rate_limiting_resettime - time.time() + 10
#             logger.info("Rate limit reached, wait for {} seconds...".format(sleep_time))
#             time.sleep(max(1.0, sleep_time))
#         except UnknownObjectException as ex:
#             logger.error("{}: {}".format(type(ex), ex))
#             break
#         except Exception as ex:
#             logger.error("{}: {}".format(type(ex), ex))
#             time.sleep(5)
#     return default


class RepoFetcher(object):
    """Fetches repository data from GitHub"""

    def __init__(self, tokens: List[str], owner: str, name: str):

        self.tokens = tokens
        self.current_token_index = 0
        self.gh = self.create_github_client()
        self.owner = owner
        self.name = name

        # self.gh = Github(token)  # 创建了一个 Github 对象，封装了对 GitHub API 的访问
        self.gh.per_page = 100  # minimize rate limit consumption
        self.repo = self.request_github(lambda: self.gh.get_repo(f"{owner}/{name}"))
        self.owner = self.repo.owner.login
        self.name = self.repo.name
        self.rate_remaining, self.rate_limit = self.request_github(lambda: self.gh.rate_limiting)
        self.rate_consumed = 0
        self.created_at = self.repo.created_at.astimezone(timezone.utc)


    def create_github_client(self):
        """Create a Github client using the current token."""
        return Github(self.tokens[self.current_token_index])

    def request_github(self, gh_func: Callable[..., T], params: Tuple = (), default: Any = None) -> Optional[T]:
        """A wrapper to handle API requests with rate limit handling and retries."""
        retries = 3
        while retries > 0:
            try:
                return gh_func(*params)
            except RateLimitExceededException as ex:
                logging.info(f"{type(ex)}: {ex}")
                # sleep_time = self.gh.rate_limiting_resettime - time.time() + 10
                logging.info(f"Rate limit reached, Preparing to switch tokens...")
                # logging.info(f"Rate limit reached, wait for {sleep_time} seconds...")
                # time.sleep(max(1.0, sleep_time))
                time.sleep(1)
                self.rotate_token()  # Rotate token on the last retry failure
            except UnknownObjectException as ex:
                logging.error(f"{type(ex)}: {ex}")
                break
            except Exception as ex:
                logging.error(f"{type(ex)}: {ex}")
                time.sleep(5)
            retries -= 1
        return default

    def rotate_token(self):
        """Rotate to the next available token."""
        self.current_token_index = (self.current_token_index + 1) % len(self.tokens)
        self.gh = self.create_github_client()  # Update the Github client with the new token
        print(f"Using new token: {self.tokens[self.current_token_index]}")
        # Re-fetch the repository and update related attributes
        self.gh.per_page = 100
        self.repo = self.request_github(lambda: self.gh.get_repo(f"{self.owner}/{self.name}"))
        self.owner = self.repo.owner.login
        self.name = self.repo.name
        self.created_at = self.repo.created_at.astimezone(timezone.utc)
        print(f"Switched to new token: {self.tokens[self.current_token_index]}")


    @property
    def rate(self) -> Tuple[int, int, int]:
        return (self.rate_remaining, self.rate_limit, self.rate_consumed)

    def _update_rate_stats(self) -> None:   # 拿到API各指标的状态，剩余量rate_remaining不会叠加
        prev = self.rate_remaining
        self.rate_remaining, self.rate_limit = self.request_github(
            lambda: self.gh.rate_limiting
        )
        if prev >= self.rate_remaining:
            self.rate_consumed += prev - self.rate_remaining
        else:
            self.rate_consumed += prev + self.rate_limit - self.rate_remaining

    def get_stats(self) -> dict[str, Any]:  # 收集仓库的详细信息
        results = self.request_github(
            lambda: {
                "owner": self.repo.owner.login,
                "name": self.repo.name,
                "language": self.repo.language,
                "languages": self.repo.get_languages(),
                "repo_created_at": self.created_at,
                "description": self.repo.description,
                "topics": self.repo.get_topics(),
                "readme": self.repo.get_readme().decoded_content.decode(
                    "utf-8", "ignore"
                ),
            },
        )
        self._update_rate_stats()
        return results

    def get_stars(self, since: datetime) -> List[dict[str, Any]]:   # 只返回在since日期之后发生的星标事件-》返回有关星标者和星标时间的字典。
        results = []
        stars = self.request_github(
            lambda: self.repo.get_stargazers_with_dates(), default=[]
        ).reversed
        page_num = self.request_github(
            lambda: get_page_num(self.gh.per_page, stars.totalCount), default=0
        )
        for p in range(0, page_num):
            logger.debug("star page %d/%d, rate %s", p, page_num, self.gh.rate_limiting)
            for star in self.request_github(self.gh, stars.get_page, (p,), []):
                starred_at = star.starred_at.astimezone(timezone.utc)
                results.append(
                    {
                        "owner": self.owner,
                        "name": self.name,
                        "user": star.user.login,
                        "starred_at": starred_at,
                    }
                )
                if starred_at < since:
                    return results
        self._update_rate_stats()
        return results

    def get_commits_in_month(self, date: datetime) -> dict[str, Any]:  # 返回指定日期范围内的提交总数
        since, until = get_month_interval(date)
        results = self.request_github(
            lambda: self.repo.get_commits(since=since, until=until).totalCount,
            default=0,
        )
        self._update_rate_stats()
        return results

    def get_commits(self, since: datetime) -> List[dict[str, Any]]:
        results = []
        commits = self.request_github(
            lambda: self.repo.get_commits(since=since), default=[]
        )
        page_num = self.request_github(
            lambda: get_page_num(self.gh.per_page, commits.totalCount),
            default=0,
        )
        for p in range(0, page_num):
            logger.debug(
                "commit page %d/%d, rate %s", p, page_num, self.gh.rate_limiting
            )
            for commit in self.request_github(commits.get_page, (p,), []):
                try:
                    author = commit.author.login
                except:
                    author = None
                try:
                    committer = commit.committer.login
                except:
                    committer = None
                results.append(
                    {
                        "owner": self.owner,
                        "name": self.name,
                        "sha": commit.sha,
                        "author": author,
                        "authored_at": commit.commit.author.date.astimezone(
                            timezone.utc
                        ),
                        "committer": committer,
                        "committed_at": commit.commit.committer.date.astimezone(
                            timezone.utc
                        ),
                        "message": commit.commit.message,
                    }
                )
        self._update_rate_stats()
        return results

    def get_issue(self,number:int):
        results = []
        issue = self.request_github(lambda: self.repo.get_issue(number),)
        if issue.state == "closed" and issue.closed_at is not None:
            closed_at = issue.closed_at.astimezone(timezone.utc)
        else:
            closed_at = None
        is_pull = issue._pull_request != NotSet  # avoid rate limit
        # issue和pr通常共享相同的数据结构，因为PR在GitHub的数据模型中本质上是一种特殊类型的 issue。
        if is_pull:
            merged_at = issue.pull_request.raw_data["merged_at"]
            if merged_at is not None:
                merged_at = parse_date(merged_at).astimezone(timezone.utc)
        else:
            merged_at = None
        results.append(
            {
                "owner": self.owner,
                "name": self.name,
                "number": issue.number,
                "user": issue.user.login,
                "state": issue.state,
                "created_at": issue.created_at.astimezone(timezone.utc),
                "closed_at": closed_at,
                "title": issue.title,
                "body": issue.body,
                "labels": [i.name for i in issue.labels],
                "is_pull": is_pull,
                "merged_at": merged_at,
            }
        )
        self._update_rate_stats()
        return results

    def get_issues(self, since: datetime) -> List[dict[str, Any]]:  # 需要加入event属性
        results = []
        issues = self.request_github(
            lambda: self.repo.get_issues(since=since, direction="asc", state="all"),
            default=[],
        )
        print("github issues get begin split page,have %d issues !!!!",issues.totalCount)
        page_num = self.request_github(
            lambda: get_page_num(self.gh.per_page, issues.totalCount),
            default=0,
        )  # 由于 GitHub API 对单次请求返回的数据有限制，对于大量数据的请求，需要通过分页来获取。
        for p in range(0, page_num):
            logger.debug(
                "issue page %d/%d, rate %s", p, page_num, self.gh.rate_limiting
            )
            for issue in self.request_github(issues.get_page, (p,), []):
                if issue.state == "closed" and issue.closed_at is not None:
                    closed_at = issue.closed_at.astimezone(timezone.utc)
                else:
                    closed_at = None
                is_pull = issue._pull_request != NotSet  # avoid rate limit
                # issue和pr通常共享相同的数据结构，因为PR在GitHub的数据模型中本质上是一种特殊类型的 issue。
                if is_pull:
                    merged_at = issue.pull_request.raw_data["merged_at"]
                    if merged_at is not None:
                        merged_at = parse_date(merged_at).astimezone(timezone.utc)
                else:
                    merged_at = None
                results.append(
                    {
                        "owner": self.owner,
                        "name": self.name,
                        "number": issue.number,
                        "user": issue.user.login,
                        "state": issue.state,
                        "created_at": issue.created_at.astimezone(timezone.utc),
                        "closed_at": closed_at,
                        "title": issue.title,
                        "body": issue.body,
                        "labels": [i.name for i in issue.labels],
                        "is_pull": is_pull,
                        "merged_at": merged_at,
                    }
                )
        self._update_rate_stats()
        return results

    def get_issue_with_retries(self, number):
        """尝试多次获取issue，如果失败则抛出异常."""
        attempts = 3
        while attempts > 0:
            issue = self.request_github(self.repo.get_issue, (number,))
            if issue is not None:
                return issue
            attempts -= 1
            logger.info(f"issue #{number} not found, #{attempts} attempts left ")
            time.sleep(3)  # 等待10秒再重试，避免立即重试导致的连续失败
            print("等待3秒再重试")

        # 如果三次尝试后仍然失败，则抛出异常
        raise ValueError(f"issue #{number} not found after 3 attempts")

    def get_issue_detail(self, number: int) -> Dict[str, Any]:
        issue = self.get_issue_with_retries(number)
        # if issue is None:
        #     raise ValueError(f"issue #{number} not found")
        events = []
        timeline_events = self.request_github(issue.get_timeline, default=[])  # timeline可以拿到每个issue的各个事件
        page_num = self.request_github(
            lambda: get_page_num(self.gh.per_page, timeline_events.totalCount),
            default=0,
        )
        resolver = {"assignee": [],
                    "pr": [],
                    "commenter": []}
        for p in range(0, page_num):
            for event in self.request_github(timeline_events.get_page, (p,), []):
                event = event.raw_data
                additional_props = {}  # 相当于event事件的额外补充说明
                if event["event"] in ["assigned", "unassigned"]:
                    if event["assignee"] is not None:
                        additional_props["assignee"] = event["assignee"]["login"]
                        # 1、通过assign直接确定resolver
                        resolver["assignee"].append(event["assignee"]["login"])
                elif event["event"] in ["labeled", "unlabeled"]:
                    additional_props["label"] = event["label"]["name"]
                elif event["event"] == "cross-referenced":
                    additional_props["source"] = event["source"]["issue"]["number"]  # 解决该issue的PR_number或者其他引用这个issue的issue_number  可以通过"pull_request"这个字段来判断这个issue是否是PR？
                    # 2、 通过PR找到resolver
                    query = Q(name=self.name, owner=self.owner)
                    resolved_issue_pr = RepoIssue.objects(
                        query & Q(is_pull=True, state="closed", number=additional_props["source"])
                    ).first()
                    if resolved_issue_pr:  # 因为存在有的cross-referenced不是pr的情况
                        resolver["pr"].append(resolved_issue_pr.user)
                elif event["event"] == "commented":
                    additional_props["comment"] = event["body"]
                    additional_props["commenter"] = event["user"]["login"]
                    # 3、 如果assign、PR都没有，则属于讨论类issue，将所有commenter都视为潜在resolver
                    resolver["commenter"].append(additional_props["commenter"])
                elif event["event"] == "referenced":
                    additional_props["commit"] = event["commit_id"]
                if "created_at" in event and event["created_at"] is not None:
                    t = parse_date(event["created_at"]).astimezone(timezone.utc)
                else:
                    t = None
                if "actor" in event and event["actor"] is not None:
                    actor = event["actor"]["login"]
                else:
                    actor = None
                events.append(
                    {
                        "type": event["event"],
                        "time": t,
                        "actor": actor,
                        **additional_props,
                    }
                )

        # Remove duplicates from each list in resolver
        resolver["assignee"] = list(set(resolver["assignee"]))
        resolver["pr"] = list(set(resolver["pr"]))
        resolver["commenter"] = list(set(resolver["commenter"]))

        if resolver["assignee"]:
            resolver = resolver["assignee"]
        elif resolver["pr"]:
            resolver = resolver["pr"]
        elif resolver["commenter"]:
            resolver = resolver["commenter"]
        else:
            resolver = []  # 如果都为空，返回一个空列表
        self._update_rate_stats()
        return {
            "owner": self.owner,
            "name": self.name,
            "number": number,
            "events": events,
            "resolver": resolver
        }

    def get_pr_with_retries(self, number):
        """尝试多次获取issue，如果失败则抛出异常."""
        attempts = 3
        while attempts > 0:
            pr = self.request_github(self.repo.get_pull, (number,))
            if pr is not None:
                return pr
            attempts -= 1
            logger.info(f"pr #{number} not found, #{attempts} attempts left ")
            time.sleep(3)  # 等待10秒再重试，避免立即重试导致的连续失败
            print("等待3秒再重试")

        # 如果三次尝试后仍然失败，则抛出异常
        raise ValueError(f"pr #{number} not found after 3 attempts")

    def get_pr_detail(self, number: int) -> Dict[str, Any]:
        pr = self.get_pr_with_retries(number)
        if pr is None:
            raise ValueError(f"pull request #{number} not found")
        # 获取所有reviewers
        reviews = self.request_github(pr.get_reviews)
        # 获取所有审查相关的评论
        review_comments = self.request_github(pr.get_review_comments)
        comment_map = defaultdict(list)
        for comment in review_comments:
            if comment.user:
                comment_map[comment.user.login].append({"time": comment.created_at.astimezone(timezone.utc),"comment": comment.body})
        # 结合审查者名单与评论信息
        reviewer_events = []
        for review in reviews:
            if review.user is None:
                continue
            # 检查该审查者是否有评论
            comments = comment_map.get(review.user.login)
            if comments:
                for comment in comments:
                    reviewer_events.append({
                        "type": "review_comment",
                        "time": comment["time"],
                        "actor": review.user.login,
                        "comment": comment["comment"]
                    })
            else:
                reviewer_events.append({
                    "type": "review_comment",
                    "time": None,
                    "actor": review.user.login,
                    "comment": None
                })

        # 普通评论事件
        normal_commenter_events = []
        normal_comments = self.request_github(pr.get_issue_comments)
        for comment in normal_comments:
            normal_commenter_events.append({
                "type": "normal_comment",
                "time": comment.created_at.astimezone(timezone.utc),
                "actor": comment.user.login if comment.user else None,
                "comment": comment.body,
            })

        # 标签者信息
        label_events = []
        pr_events = self.request_github(pr.get_issue_events, default=[])  # get_issue_events可以拿到pr的某些事件
        page_num = self.request_github(
            lambda: get_page_num(self.gh.per_page, pr_events.totalCount),
            default=0,
        )
        for p in range(0, page_num):
            for event in self.request_github(pr_events.get_page, (p,), []):
                event_raw_data = event.raw_data
                # retries = 3
                # while retries > 0:
                #     try:
                #         event_raw_data = event.raw_data
                #         break
                #     except RateLimitExceededException as ex:
                #         logging.info(f"{type(ex)}: {ex}")
                #         logging.info(f"Rate limit reached, Preparing to switch tokens...")
                #         time.sleep(10)
                #         self.rotate_token()  # Rotate token on the last retry failure
                #     except UnknownObjectException as ex:
                #         logging.error(f"{type(ex)}: {ex}")
                #         break
                #     except Exception as ex:
                #         logging.error(f"{type(ex)}: {ex}")
                #         time.sleep(5)
                #     retries -= 1
                if event_raw_data["event"] in ["labeled", "unlabeled"]:
                    if "label" in event_raw_data and event_raw_data["label"]["name"] is not None:
                        comment = event_raw_data["label"]["name"]
                    else:
                        comment = None
                    if "created_at" in event_raw_data and event_raw_data["created_at"] is not None:
                        create_time = parse_date(event_raw_data["created_at"]).astimezone(timezone.utc)
                    else:
                        create_time = None
                    if "actor" in event_raw_data and event_raw_data["actor"] is not None:
                        actor = event_raw_data["actor"]["login"]
                    else:
                        actor = None
                    label_events.append(
                        {
                            "type": event_raw_data["event"],
                            "time": create_time,
                            "actor": actor,
                            "comment": comment,
                        }
                    )

        # 完善pr的信息
        results = {
            "owner": self.owner,
            "name": self.name,
            "number": number,
            "reviewer_events": reviewer_events,
            "normal_commenter_events": normal_commenter_events,
            "label_events": label_events,
        }
        self._update_rate_stats()
        return results
