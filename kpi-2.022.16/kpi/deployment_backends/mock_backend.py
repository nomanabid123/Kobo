# coding: utf-8
import copy
import os
import re
import time
import uuid
from datetime import datetime
from typing import Optional, Union
from xml.etree import ElementTree as ET

import pytz
from deepmerge import always_merger
from dicttoxml import dicttoxml
from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext as t
from lxml import etree
from rest_framework import status

from kpi.constants import (
    SUBMISSION_FORMAT_TYPE_JSON,
    SUBMISSION_FORMAT_TYPE_XML,
    PERM_CHANGE_SUBMISSIONS,
    PERM_DELETE_SUBMISSIONS,
    PERM_VALIDATE_SUBMISSIONS,
)
from kpi.exceptions import (
    AttachmentNotFoundException,
    InvalidXPathException,
    SubmissionNotFoundException,
    XPathNotFoundException,
)
from kpi.interfaces.sync_backend_media import SyncBackendMediaInterface
from kpi.models.asset_file import AssetFile
from kpi.tests.utils.mock import MockAttachment
from kpi.utils.mongo_helper import MongoHelper, drop_mock_only
from kpi.utils.xml import edit_submission_xml
from .base_backend import BaseDeploymentBackend
from ..exceptions import KobocatBulkUpdateSubmissionsClientException


class MockDeploymentBackend(BaseDeploymentBackend):
    """
    Only used for unit testing and interface testing.
    """

    PROTECTED_XML_FIELDS = [
        '__version__',
        'formhub',
        'meta',
    ]

    def bulk_assign_mapped_perms(self):
        pass

    def bulk_update_submissions(
        self, data: dict, user: 'auth.User'
    ) -> dict:
        submission_ids = self.validate_access_with_partial_perms(
            user=user,
            perm=PERM_CHANGE_SUBMISSIONS,
            submission_ids=data['submission_ids'],
            query=data['query'],
        )

        if submission_ids:
            data['query'] = {}
        else:
            submission_ids = data['submission_ids']

        submissions = self.get_submissions(
            user=user,
            format_type=SUBMISSION_FORMAT_TYPE_XML,
            submission_ids=submission_ids,
            query=data['query'],
        )

        if not self.current_submissions_count:
            raise KobocatBulkUpdateSubmissionsClientException(
                detail=t('No submissions match the given `submission_ids`')
            )

        update_data = self.__prepare_bulk_update_data(data['data'])
        kc_responses = []
        for submission in submissions:
            # Remove XML declaration from submission
            submission = re.sub(r'(<\?.*\?>)', '', submission)
            xml_parsed = etree.fromstring(submission)

            _uuid, uuid_formatted = self.generate_new_instance_id()

            instance_id = xml_parsed.find('meta/instanceID')
            deprecated_id = xml_parsed.find('meta/deprecatedID')
            deprecated_id_or_new = (
                deprecated_id
                if deprecated_id is not None
                else etree.SubElement(xml_parsed.find('meta'), 'deprecatedID')
            )
            deprecated_id_or_new.text = instance_id.text
            instance_id.text = uuid_formatted

            for path, value in update_data.items():
                edit_submission_xml(xml_parsed, path, value)

            kc_responses.append(
                {
                    'uuid': _uuid,
                    'status_code': status.HTTP_201_CREATED,
                    'message': 'Successful submission',
                    'updated_submission': etree.tostring(xml_parsed) # only for testing
                }
            )

        return self.__prepare_bulk_update_response(kc_responses)

    def calculated_submission_count(self, user: 'auth.User', **kwargs) -> int:
        params = self.validate_submission_list_params(user,
                                                      validate_count=True,
                                                      **kwargs)
        return MongoHelper.get_count(self.mongo_userform_id, **params)

    def connect(self, active=False):
        self.store_data({
            'backend': 'mock',
            'identifier': 'mock://%s' % self.asset.uid,
            'active': active,
            'backend_response': {
                'downloadable': active,
                'has_kpi_hook': self.asset.has_active_hooks,
                'kpi_asset_uid': self.asset.uid
            }
        })

    @drop_mock_only
    def delete_submission(self, submission_id: int, user: 'auth.User') -> dict:
        """
        Delete a submission
        """
        self.validate_access_with_partial_perms(
            user=user,
            perm=PERM_DELETE_SUBMISSIONS,
            submission_ids=[submission_id],
        )

        if not settings.MONGO_DB.instances.find_one({'_id': submission_id}):
            return {
                'content_type': 'application/json',
                'status': status.HTTP_404_NOT_FOUND,
                'data': {
                    'detail': 'Not found'
                }
            }

        settings.MONGO_DB.instances.delete_one({'_id': submission_id})

        return {
            'content_type': 'application/json',
            'status': status.HTTP_204_NO_CONTENT,
        }

    def delete_submissions(self, data: dict, user: 'auth.User') -> dict:
        """
        Bulk delete provided submissions authenticated by `user`'s API token.

        `data` should contains the submission ids or the query to get the subset
        of submissions to delete
        Example:
             {"submission_ids": [1, 2, 3]}
             or
             {"query": {"Question": "response"}
        """
        submission_ids = self.validate_access_with_partial_perms(
            user=user,
            perm=PERM_DELETE_SUBMISSIONS,
            submission_ids=data['submission_ids'],
            query=data['query'],
        )

        if not submission_ids:
            submission_ids = data['submission_ids']
        else:
            data['query'] = {}

        # Retrieve the subset of submissions to delete
        submissions = self.get_submissions(user,
                                           submission_ids=submission_ids,
                                           query=data['query'])

        # If no submissions have been fetched, user is not allowed to perform
        # the request
        if not submissions:
            return {
                'content_type': 'application/json',
                'status': status.HTTP_404_NOT_FOUND,
            }

        # We could use `delete_many()` but we would have to recreate the query
        # with submission ids or query.
        for submission in submissions:
            submission_id = submission['_id']
            settings.MONGO_DB.instances.delete_one(
                {'_id': submission_id}
            )

        return {
            'content_type': 'application/json',
            'status': status.HTTP_204_NO_CONTENT,
        }

    def duplicate_submission(
        self, submission_id: int, user: 'auth.User'
    ) -> dict:
        # TODO: Make this operate on XML somehow and reuse code from
        # KobocatDeploymentBackend, to catch issues like #3054

        self.validate_access_with_partial_perms(
            user=user,
            perm=PERM_CHANGE_SUBMISSIONS,
            submission_ids=[submission_id],
        )

        duplicated_submission = copy.deepcopy(
            self.get_submission(submission_id, user=user)
        )
        updated_time = datetime.now(tz=pytz.UTC).isoformat('T', 'milliseconds')
        next_id = max((
            sub['_id']
            for sub in self.get_submissions(self.asset.owner, fields=['_id'])
        )) + 1
        duplicated_submission.update({
            '_id': next_id,
            'start': updated_time,
            'end': updated_time,
            'meta/instanceID': f'uuid:{uuid.uuid4()}'
        })
        
        settings.MONGO_DB.instances.insert_one(duplicated_submission)
        return duplicated_submission

    def get_attachment(
        self,
        submission_id_or_uuid: Union[int, str],
        user: 'auth.User',
        attachment_id: Optional[int] = None,
        xpath: Optional[str] = None,
    ) -> MockAttachment:

        submission_json = None
        # First try to get the json version of the submission.
        # It helps to retrieve the id if `submission_id_or_uuid` is a `UUIDv4`
        try:
            submission_id_or_uuid = int(submission_id_or_uuid)
        except ValueError:
            submissions = self.get_submissions(
                user,
                format_type=SUBMISSION_FORMAT_TYPE_JSON,
                query={'_uuid': submission_id_or_uuid},
            )
            if submissions:
                submission_json = submissions[0]
        else:
            submission_json = self.get_submission(
                submission_id_or_uuid, user, format_type=SUBMISSION_FORMAT_TYPE_JSON
            )

        if not submission_json:
            raise SubmissionNotFoundException

        submission_xml = self.get_submission(
            submission_json['_id'], user, format_type=SUBMISSION_FORMAT_TYPE_XML
        )

        if xpath:
            submission_tree = ET.ElementTree(
                ET.fromstring(submission_xml)
            )
            try:
                element = submission_tree.find(xpath)
            except KeyError:
                raise InvalidXPathException

            try:
                attachment_filename = element.text
            except AttributeError:
                raise XPathNotFoundException

        attachments = submission_json['_attachments']
        for attachment in attachments:
            filename = os.path.basename(attachment['filename'])

            if xpath:
                is_good_file = attachment_filename == filename
            else:
                is_good_file = int(attachment['id']) == int(attachment_id)

            if is_good_file:
                return MockAttachment(pk=attachment_id, **attachment)

        raise AttachmentNotFoundException

    def get_attachment_objects_from_dict(self, submission: dict) -> list:

        if not submission.get('_attachments'):
            return []

        return [
            MockAttachment(pk=attachment['id'], **attachment)
            for attachment in attachments
        ]

    def get_data_download_links(self):
        return {}

    def get_enketo_survey_links(self):
        # `self` is a demo Enketo form, but there's no guarantee it'll be
        # around forever.
        return {
            'offline_url': 'https://enke.to/_/#self',
            'url': 'https://enke.to/::self',
            'iframe_url': 'https://enke.to/i/::self',
            'preview_url': 'https://enke.to/preview/::self',
            # 'preview_iframe_url': 'https://enke.to/preview/i/::self',
        }

    def get_submission_detail_url(self, submission_id: int) -> str:
        # This doesn't really need to be implemented.
        # We keep it to stay close to `KobocatDeploymentBackend`
        url = f'{self.submission_list_url}{submission_id}/'
        return url

    def get_submission_validation_status_url(self, submission_id: int) -> str:
        url = '{detail_url}validation_status/'.format(
            detail_url=self.get_submission_detail_url(submission_id)
        )
        return url

    def get_submissions(
        self,
        user: 'auth.User',
        format_type: str = SUBMISSION_FORMAT_TYPE_JSON,
        submission_ids: list = [],
        request: Optional['rest_framework.request.Request'] = None,
        **mongo_query_params
    ) -> list:
        """
        Retrieve submissions that `user` is allowed to access.

        The format `format_type` can be either:
        - 'json' (See `kpi.constants.SUBMISSION_FORMAT_TYPE_JSON`)
        - 'xml' (See `kpi.constants.SUBMISSION_FORMAT_TYPE_XML`)

        Results can be filtered by submission ids. Moreover MongoDB filters can
        be passed through `mongo_query_params` to narrow down the results.

        If `user` has no access to these submissions or no matches are found,
        an empty list is returned.
        If `format_type` is 'json', a list of dictionaries is returned.
        Otherwise, if `format_type` is 'xml', a list of strings is returned.
        """

        mongo_query_params['submission_ids'] = submission_ids
        params = self.validate_submission_list_params(user,
                                                      format_type=format_type,
                                                      **mongo_query_params)

        mongo_cursor, total_count = MongoHelper.get_instances(
            self.mongo_userform_id, **params)

        # Python-only attribute used by `kpi.views.v2.data.DataViewSet.list()`
        self.current_submissions_count = total_count

        submissions = [
            MongoHelper.to_readable_dict(submission)
            for submission in mongo_cursor
        ]

        if format_type != SUBMISSION_FORMAT_TYPE_XML:
            return submissions

        return [
            dicttoxml(
                self.__prepare_xml(submission),
                attr_type=False,
                custom_root=self.asset.uid,
            ).decode()
            for submission in submissions
        ]

    def get_validation_status(self, submission_id: int, user: 'auth.User') -> dict:

        submission = self.get_submission(submission_id, user)
        return {
            'content_type': 'application/json',
            'data': submission.get('_validation_status')
        }

    @drop_mock_only
    def mock_submissions(self, submissions: list, flush_db: bool = True):
        """
        Insert dummy submissions into deployment data
        """
        if flush_db:
            settings.MONGO_DB.instances.drop()
        count = settings.MONGO_DB.instances.count_documents({})

        for idx, submission in enumerate(submissions):
            submission[MongoHelper.USERFORM_ID] = self.mongo_userform_id
            # Some data already provide `_id`. Use it if it is present.
            # There could be conflicts if some submissions come with an id
            # or others do not.
            # MockMongo will raise a DuplicateKey error
            if '_id' not in submission:
                submission['_id'] = count + idx + 1
            settings.MONGO_DB.instances.insert_one(submission)
            # Do not add `MongoHelper.USERFORM_ID` to original `submissions`
            del submission[MongoHelper.USERFORM_ID]

    @property
    def mongo_userform_id(self):
        return f'{self.asset.owner.username}_{self.asset.uid}'

    def redeploy(self, active: bool = None):
        """
        Replace (overwrite) the deployment, keeping the same identifier, and
        optionally changing whether the deployment is active
        """
        if active is None:
            active = self.active

        self.store_data({
            'active': active,
            'version': self.asset.version_id,
        })

        self.set_asset_uid()

    def set_active(self, active: bool):
        self.save_to_db({
            'active': bool(active),
        })

    def set_asset_uid(self, **kwargs) -> bool:
        backend_response = self.backend_response
        backend_response.update({
            'kpi_asset_uid': self.asset.uid,
        })
        self.store_data({
            'backend_response': backend_response
        })

    def set_has_kpi_hooks(self):
        """
        Store a boolean which indicates that KPI has active hooks (or not)
        and, if it is the case, it should receive notifications when new data
        comes in
        """
        has_active_hooks = self.asset.has_active_hooks
        self.store_data({
            'has_kpi_hooks': has_active_hooks,
        })

    def set_namespace(self, namespace):
        self.store_data({
            'namespace': namespace,
        })

    def set_validation_status(self,
                              submission_id: int,
                              user: 'auth.User',
                              data: dict,
                              method: str) -> dict:

        self.validate_access_with_partial_perms(
            user=user,
            perm=PERM_VALIDATE_SUBMISSIONS,
            submission_ids=[submission_id],
        )

        validation_status = {}
        status_code = status.HTTP_204_NO_CONTENT

        if method != 'DELETE':
            validation_status = {
                'timestamp': int(time.time()),
                'uid': data['validation_status.uid'],
                'by_whom': user.username,
            }
            status_code = status.HTTP_200_OK

        settings.MONGO_DB.instances.update_one(
            {'_id': submission_id},
            {'$set': {'_validation_status': validation_status}},
        )
        return {
            'content_type': 'application/json',
            'status': status_code,
            'data': validation_status
        }

    def set_validation_statuses(self, user: 'auth.User', data: dict) -> dict:
        """
        Bulk update validation status for provided submissions.

        `data` should contains either the submission ids or the query to
        retrieve the subset of submissions chosen by then user.
        If none of them are provided, all the submissions are selected
        Examples:
            {"submission_ids": [1, 2, 3]}
            {"query":{"_validation_status.uid":"validation_status_not_approved"}
        
        """

        submission_ids = self.validate_access_with_partial_perms(
            user=user,
            perm=PERM_VALIDATE_SUBMISSIONS,
            submission_ids=data['submission_ids'],
            query=data['query'],
        )

        if not submission_ids:
            submission_ids = data['submission_ids']
        else:
            # Reset query because submission ids are provided from partial
            # perms validation
            data['query'] = {}

        submissions = self.get_submissions(
            user=user,
            submission_ids=submission_ids,
            query=data['query'],
            fields=['_id'],
        )

        submissions_count = 0

        for submission in submissions:
            if not data['validation_status.uid']:
                validation_status = {}
            else:
                validation_status = {
                    'timestamp': int(time.time()),
                    'uid': data['validation_status.uid'],
                    'by_whom': user.username,
                }
            settings.MONGO_DB.instances.update_one(
                {'_id': submission['_id']},
                {'$set': {'_validation_status': validation_status}},
            )

            submissions_count += 1

        return {
            'content_type': 'application/json',
            'status': status.HTTP_200_OK,
            'data': {
                'detail': f'{submissions_count} submissions have been updated'
            }
        }

    @staticmethod
    def generate_new_instance_id() -> (str, str):
        """
        Returns:
            - Generated uuid
            - Formatted uuid for OpenRosa xml
        """
        _uuid = str(uuid.uuid4())
        return _uuid, f'uuid:{_uuid}'

    @property
    def submission_list_url(self):
        # This doesn't really need to be implemented.
        # We keep it to stay close to `KobocatDeploymentBackend`
        view_name = 'submission-list'
        namespace = self.get_data('namespace', None)
        if namespace is not None:
            view_name = '{}:{}'.format(namespace, view_name)
        return reverse(view_name,
                       kwargs={'parent_lookup_asset': self.asset.uid})

    def sync_media_files(self, file_type: str = AssetFile.FORM_MEDIA):
        queryset = self._get_metadata_queryset(file_type=file_type)
        for obj in queryset:
            assert issubclass(obj.__class__, SyncBackendMediaInterface)

    @classmethod
    def __prepare_bulk_update_data(cls, updates: dict) -> dict:
        """
        Preparing the request payload for bulk updating of submissions
        """
        # Sanitizing the payload of potentially destructive keys
        sanitized_updates = copy.deepcopy(updates)
        for key in updates:
            if (
                key in cls.PROTECTED_XML_FIELDS
                or '/' in key and key.split('/')[0] in cls.PROTECTED_XML_FIELDS
            ):
                sanitized_updates.pop(key)

        return sanitized_updates

    @staticmethod
    def __prepare_bulk_update_response(kc_responses: list) -> dict:
        total_update_attempts = len(kc_responses)
        total_successes = total_update_attempts  # all will be successful
        return {
            'status': status.HTTP_200_OK,
            'data': {
                'count': total_update_attempts,
                'successes': total_successes,
                'failures': total_update_attempts - total_successes,
                'results': kc_responses,
            },
        }

    @staticmethod
    def __prepare_xml(submission: dict) -> dict:
        submission_copy = copy.deepcopy(submission)

        for k, v in submission_copy.items():
            if '/' not in k:
                continue
            value = v
            for key in reversed(k.strip('/').split('/')):
                value = {key: value}
            always_merger.merge(submission, value)
            del submission[k]

        return submission
