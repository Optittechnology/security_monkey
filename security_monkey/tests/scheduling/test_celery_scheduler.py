#     Copyright 2018 Netflix, Inc.
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
"""
.. module: security_monkey.tests.scheduling.test_celery_scheduler
    :platform: Unix
.. version:: $$VERSION$$
.. moduleauthor::  Mike Grima <mgrima@netflix.com>
"""
import json

import boto3
import mock
from mock import patch
from moto import mock_iam, mock_sts
from pytest import raises

from security_monkey import db, ARN_PREFIX
from security_monkey.datastore import Account, AccountType, Technology, Item, ItemAudit, ItemRevision
from security_monkey.monitors import Monitor
from security_monkey.tests import SecurityMonkeyTestCase
from security_monkey.watcher import Watcher

OPEN_POLICY = {
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "*",
            "Resource": "*"
        }
    ]
}

ROLE_CONF = {
    "account_number": "012345678910",
    "technology": "iamrole",
    "region": "universal",
    "name": "roleNumber",
    "InlinePolicies": {"ThePolicy": OPEN_POLICY},
    "Arn": ARN_PREFIX + ":iam::012345678910:role/roleNumber"
}


class CelerySchedulerTestCase(SecurityMonkeyTestCase):
    test_account1 = None
    test_account2 = None
    test_account3 = None
    test_account4 = None

    def pre_test_setup(self):
        account_type_result = AccountType(name='AWS')
        db.session.add(account_type_result)
        db.session.commit()

        account = Account(identifier="012345678910", name="TEST_ACCOUNT1",
                          account_type_id=account_type_result.id, notes="TEST_ACCOUNT1",
                          third_party=False, active=True)
        db.session.add(account)

        account = Account(identifier="123123123123", name="TEST_ACCOUNT2",
                          account_type_id=account_type_result.id, notes="TEST_ACCOUNT2",
                          third_party=False, active=True)
        db.session.add(account)

        account = Account(identifier="109876543210", name="TEST_ACCOUNT3",
                          account_type_id=account_type_result.id, notes="TEST_ACCOUNT3",
                          third_party=False, active=False)
        db.session.add(account)

        account = Account(identifier="456456456456", name="TEST_ACCOUNT4",
                          account_type_id=account_type_result.id, notes="TEST_ACCOUNT4",
                          third_party=False, active=False)
        db.session.add(account)

        db.session.commit()

    @patch("security_monkey.task_scheduler.tasks.fix_orphaned_deletions")
    def test_find_batch_changes(self, mock_fix_orphaned):
        """
        Runs through a full find job via the IAMRole watcher, as that supports batching.

        However, this is mostly testing the logic through each function call -- this is
        not going to do any boto work and that will instead be mocked out.
        :return:
        """
        from security_monkey.task_scheduler.tasks import manual_run_change_finder
        from security_monkey.monitors import Monitor
        from security_monkey.watchers.iam.iam_role import IAMRole
        from security_monkey.auditors.iam.iam_role import IAMRoleAuditor
        test_account = Account(name="TEST_ACCOUNT1")
        watcher = IAMRole(accounts=[test_account.name])

        technology = Technology(name="iamrole")
        db.session.add(technology)
        db.session.commit()

        watcher.batched_size = 3  # should loop 4 times

        self.add_roles()

        # Set up the monitor:
        batched_monitor = Monitor(IAMRole, test_account)
        batched_monitor.watcher = watcher
        batched_monitor.auditors = [IAMRoleAuditor(accounts=[test_account.name])]

        import security_monkey.task_scheduler.tasks
        old_get_monitors = security_monkey.task_scheduler.tasks.get_monitors
        security_monkey.task_scheduler.tasks.get_monitors = lambda x, y, z: [batched_monitor]

        # Moto screws up the IAM Role ARN -- so we need to fix it:
        original_slurp_list = watcher.slurp_list
        original_slurp = watcher.slurp

        def mock_slurp_list():
            items, exception_map = original_slurp_list()

            for item in watcher.total_list:
                item["Arn"] = ARN_PREFIX + ":iam::012345678910:role/{}".format(item["RoleName"])

            return items, exception_map

        def mock_slurp():
            batched_items, exception_map = original_slurp()

            for item in batched_items:
                item.arn = ARN_PREFIX + ":iam::012345678910:role/{}".format(item.name)
                item.config["Arn"] = item.arn
                item.config["RoleId"] = item.name  # Need this to stay the same

            return batched_items, exception_map

        watcher.slurp_list = mock_slurp_list
        watcher.slurp = mock_slurp

        manual_run_change_finder([test_account.name], [watcher.index])
        assert mock_fix_orphaned.called

        # Check that all items were added to the DB:
        assert len(Item.query.all()) == 11

        # Check that we have exactly 11 item revisions:
        assert len(ItemRevision.query.all()) == 11

        # Check that there are audit issues for all 11 items:
        assert len(ItemAudit.query.all()) == 11

        # Delete one of the items:
        # Moto lacks implementation for "delete_role" (and I'm too lazy to submit a PR :D) -- so need to create again...
        mock_iam().stop()
        mock_sts().stop()
        self.add_roles(initial=False)

        # Run the it again:
        watcher.current_account = None  # Need to reset the watcher
        manual_run_change_finder([test_account.name], [watcher.index])

        # Check that nothing new was added:
        assert len(Item.query.all()) == 11

        # There should be the same number of issues and 2 more revisions:
        assert len(ItemAudit.query.all()) == 11
        assert len(ItemRevision.query.all()) == 13

        # Check that the deleted roles show as being inactive:
        ir = ItemRevision.query.join((Item, ItemRevision.id == Item.latest_revision_id)) \
            .filter(Item.arn.in_(
                [ARN_PREFIX + ":iam::012345678910:role/roleNumber9",
                 ARN_PREFIX + ":iam::012345678910:role/roleNumber10"])).all()

        assert len(ir) == 2
        assert not ir[0].active
        assert not ir[1].active

        # Finally -- test with a slurp list exception (just checking that things don't blow up):
        import security_monkey.watchers.iam.iam_role
        old_list_roles = security_monkey.watchers.iam.iam_role.list_roles

        def mock_slurp_list_with_exception():
            security_monkey.watchers.iam.iam_role.list_roles = lambda **kwargs: 1 / 0

            items, exception_map = original_slurp_list()

            assert len(exception_map) > 0
            return items, exception_map

        watcher.slurp_list = mock_slurp_list_with_exception
        watcher.current_account = None  # Need to reset the watcher
        manual_run_change_finder([test_account.name], [watcher.index])

        security_monkey.task_scheduler.tasks.get_monitors = old_get_monitors
        security_monkey.watchers.iam.iam_role.list_roles = old_list_roles

        mock_iam().stop()
        mock_sts().stop()

    def test_audit_specific_changes(self):
        from security_monkey.task_scheduler.tasks import _audit_specific_changes
        from security_monkey.monitors import Monitor
        from security_monkey.watchers.iam.iam_role import IAMRole
        from security_monkey.cloudaux_watcher import CloudAuxChangeItem
        from security_monkey.auditors.iam.iam_role import IAMRoleAuditor

        # Set up the monitor:
        test_account = Account.query.filter(Account.name == "TEST_ACCOUNT1").one()
        batched_monitor = Monitor(IAMRole, test_account)
        batched_monitor.auditors = [IAMRoleAuditor(accounts=[test_account.name])]

        technology = Technology(name="iamrole")
        db.session.add(technology)
        db.session.commit()

        watcher = Watcher(accounts=[test_account.name])
        watcher.current_account = (test_account, 0)
        watcher.technology = technology

        # Create some IAM roles for testing:
        items = []
        for x in range(0, 3):
            role_policy = dict(ROLE_CONF)
            role_policy["Arn"] = ARN_PREFIX + ":iam::012345678910:role/roleNumber{}".format(x)
            role_policy["RoleName"] = "roleNumber{}".format(x)
            role = CloudAuxChangeItem.from_item(name=role_policy['RoleName'], item=role_policy,
                                                record_region='universal', account_name=test_account.name,
                                                index='iamrole', source_watcher=watcher)
            items.append(role)

        audit_items = watcher.find_changes_batch(items, {})
        assert len(audit_items) == 3

        # Perform the audit:
        _audit_specific_changes(batched_monitor, audit_items, False)

        # Check all the issues are there:
        assert len(ItemAudit.query.all()) == 3

    def add_roles(self, initial=True):
        mock_sts().start()
        mock_iam().start()
        client = boto3.client("iam")

        aspd = {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Principal": {
                        "Service": "ec2.amazonaws.com"
                    }
                }
            ]
        }

        if initial:
            last = 11
        else:
            last = 9  # Simulates 2 deleted roles...

        for x in range(0, last):
            # Create the IAM Role via Moto:
            aspd["Statement"][0]["Resource"] = ARN_PREFIX + ":iam:012345678910:role/roleNumber{}".format(x)
            client.create_role(Path="/", RoleName="roleNumber{}".format(x),
                               AssumeRolePolicyDocument=json.dumps(aspd, indent=4))
            client.put_role_policy(RoleName="roleNumber{}".format(x), PolicyName="testpolicy",
                                   PolicyDocument=json.dumps(OPEN_POLICY, indent=4))

    @patch("security_monkey.task_scheduler.tasks.fix_orphaned_deletions")
    def test_report_batch_changes(self, mock_fix_orphaned):
        from security_monkey.task_scheduler.tasks import manual_run_change_reporter
        from security_monkey.datastore import Item, ItemRevision, ItemAudit
        from security_monkey.monitors import Monitor
        from security_monkey.watchers.iam.iam_role import IAMRole
        from security_monkey.auditors.iam.iam_role import IAMRoleAuditor

        test_account = Account.query.filter(Account.name == "TEST_ACCOUNT1").one()

        watcher = IAMRole(accounts=[test_account.name])

        watcher.batched_size = 3  # should loop 4 times

        self.add_roles()

        # Set up the monitor:
        batched_monitor = Monitor(IAMRole, test_account)
        batched_monitor.watcher = watcher
        batched_monitor.auditors = [IAMRoleAuditor(accounts=[test_account.name])]

        # Set up the Reporter:
        import security_monkey.reporter
        old_all_monitors = security_monkey.reporter.all_monitors
        security_monkey.reporter.all_monitors = lambda x, y: [batched_monitor]

        import security_monkey.task_scheduler.tasks
        old_get_monitors = security_monkey.task_scheduler.tasks.get_monitors
        security_monkey.task_scheduler.tasks.get_monitors = lambda x, y, z: [batched_monitor]

        # Moto screws up the IAM Role ARN -- so we need to fix it:
        original_slurp_list = watcher.slurp_list
        original_slurp = watcher.slurp

        def mock_slurp_list():
            items, exception_map = original_slurp_list()

            for item in watcher.total_list:
                item["Arn"] = ARN_PREFIX + ":iam::012345678910:role/{}".format(item["RoleName"])

            return items, exception_map

        def mock_slurp():
            batched_items, exception_map = original_slurp()

            for item in batched_items:
                item.arn = ARN_PREFIX + ":iam::012345678910:role/{}".format(item.name)
                item.config["Arn"] = item.arn
                item.config["RoleId"] = item.name  # Need this to stay the same

            return batched_items, exception_map

        watcher.slurp_list = mock_slurp_list
        watcher.slurp = mock_slurp

        manual_run_change_reporter([test_account.name])

        assert mock_fix_orphaned.called

        # Check that all items were added to the DB:
        assert len(Item.query.all()) == 11

        # Check that we have exactly 11 item revisions:
        assert len(ItemRevision.query.all()) == 11

        # Check that there are audit issues for all 11 items:
        assert len(ItemAudit.query.all()) == 11

        mock_iam().stop()
        mock_sts().stop()

        security_monkey.reporter.all_monitors = old_all_monitors
        security_monkey.task_scheduler.tasks.get_monitors = old_get_monitors

    def test_celery_purge(self):
        from security_monkey.task_scheduler.beat import purge_it
        with patch("security_monkey.task_scheduler.beat.CELERY") as mock:
            purge_it()
            assert mock.control.purge.called

    def test_fix_orphaned_deletions(self):
        test_account = Account.query.filter(Account.name == "TEST_ACCOUNT1").one()
        technology = Technology(name="orphaned")

        db.session.add(technology)
        db.session.commit()

        orphaned_item = Item(name="orphaned", region="us-east-1", tech_id=technology.id, account_id=test_account.id)
        db.session.add(orphaned_item)
        db.session.commit()

        assert not orphaned_item.latest_revision_id
        assert not orphaned_item.revisions.count()
        assert len(Item.query.filter(Item.account_id == test_account.id, Item.tech_id == technology.id,
                                     Item.latest_revision_id == None).all()) == 1  # noqa

        from security_monkey.task_scheduler.tasks import fix_orphaned_deletions
        fix_orphaned_deletions(test_account.name, technology.name)

        assert not Item.query.filter(Item.account_id == test_account.id, Item.tech_id == technology.id,
                                     Item.latest_revision_id == None).all()  # noqa

        assert orphaned_item.latest_revision_id
        assert orphaned_item.revisions.count() == 1
        assert orphaned_item.latest_config == {}

    @patch("security_monkey.task_scheduler.beat.setup")
    @patch("security_monkey.task_scheduler.beat.purge_it")
    @patch("security_monkey.task_scheduler.tasks.task_account_tech")
    @patch("security_monkey.task_scheduler.tasks.task_audit")
    @patch("security_monkey.task_scheduler.tasks.clear_expired_exceptions")
    def test_celery_beat(self, mock_expired_exceptions, mock_task_audit, mock_account_tech, mock_purge, mock_setup):
        from security_monkey.task_scheduler.beat import setup_the_tasks
        from security_monkey.watchers.iam.iam_role import IAMRole
        from security_monkey.auditors.iam.iam_role import IAMRoleAuditor

        # Set up the monitor:
        test_account = Account.query.filter(Account.name == "TEST_ACCOUNT1").one()
        watcher = IAMRole(accounts=[test_account.name])
        batched_monitor = Monitor(IAMRole, test_account)
        batched_monitor.watcher = watcher
        batched_monitor.auditors = [IAMRoleAuditor(accounts=[test_account.name])]

        import security_monkey.task_scheduler.tasks
        old_get_monitors = security_monkey.task_scheduler.tasks.get_monitors
        security_monkey.task_scheduler.tasks.get_monitors = lambda x, y, z: [batched_monitor]

        setup_the_tasks(mock.Mock())

        assert mock_setup.called
        assert mock_purge.called

        # "apply_async" where the immediately scheduled tasks
        assert mock_account_tech.apply_async.called

        # The ".s" are the scheduled tasks. Too lazy to grab the intervals out.
        assert mock_account_tech.s.called
        assert mock_expired_exceptions.s.called
        assert mock_task_audit.s.called

        # Build the expected mock results:
        scheduled_tech_result_list = []
        async_result_list = []
        audit_result_list = []

        import security_monkey.watcher
        import security_monkey.auditor

        for account in Account.query.filter(Account.third_party == False).filter(Account.active == True).all():  # noqa
            for w in security_monkey.watcher.watcher_registry.iterkeys():
                scheduled_tech_result_list.append(((account.name, w),))
                async_result_list.append((((account.name, w),),))

            # It's just policy for IAM:
            audit_result_list.append(((account.name, "policy"),))

        assert mock_account_tech.s.call_args_list == scheduled_tech_result_list
        assert async_result_list == mock_account_tech.apply_async.call_args_list
        assert audit_result_list == mock_task_audit.s.call_args_list

        security_monkey.task_scheduler.tasks.get_monitors = old_get_monitors

    @patch("security_monkey.task_scheduler.tasks.clear_old_exceptions")
    def test_celery_exception_task(self, mock_exception_clear):
        from security_monkey.task_scheduler.tasks import clear_expired_exceptions
        clear_expired_exceptions()
        assert mock_exception_clear.assert_called

    @patch("security_monkey.task_scheduler.tasks.setup")
    @patch("security_monkey.task_scheduler.tasks.audit_changes")
    @patch("security_monkey.task_scheduler.tasks.store_exception")
    def test_audit_task(self, mock_store_exception, mock_audit_changes, mock_setup):
        from security_monkey.task_scheduler.tasks import task_audit

        account_name = "TEST_ACCOUNT1"
        technology_name = "iamrole"

        task_audit(account_name, technology_name)  # noqa
        mock_audit_changes.assert_called_with([account_name], [technology_name], True)

        assert mock_setup.called

        exception = Exception("Testing")
        mock_audit_changes.side_effect = exception
        with raises(Exception):
            task_audit(account_name, technology_name)  # noqa

        mock_store_exception.assert_called_with("scheduler-exception-on-audit", None, exception)

    @patch("security_monkey.task_scheduler.tasks.setup")
    @patch("security_monkey.task_scheduler.tasks.reporter_logic")
    @patch("security_monkey.task_scheduler.tasks.store_exception")
    def test_account_tech_task(self, mock_store_exception, mock_reporter, mock_setup):
        from security_monkey.task_scheduler.tasks import task_account_tech

        account_name = "TEST_ACCOUNT1"
        technology_name = "iamrole"

        task_account_tech(account_name, technology_name)  # noqa
        mock_reporter.assert_called_with(account_name, technology_name)

        assert mock_setup.called

        exception = Exception("Testing")
        mock_reporter.side_effect = exception
        with raises(Exception):
            task_account_tech(account_name, technology_name)  # noqa

        mock_store_exception.assert_called_with("scheduler-exception-on-watch", None, exception)
