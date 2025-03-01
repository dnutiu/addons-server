import datetime
import re

from django.conf import settings

import olympia.core.logger
from olympia import amo
from olympia.activity import log_create
from olympia.lib.kinto import KintoServer


log = olympia.core.logger.getLogger('z.amo.blocklist')

KINTO_COLLECTION_LEGACY = 'addons'
KINTO_COLLECTION_MLBF = 'addons-bloomfilters'


def add_version_log_for_blocked_versions(obj, al):
    from olympia.activity.models import VersionLog

    VersionLog.objects.bulk_create([
        VersionLog(activity_log=al, version_id=id_)
        for version, (id_, _) in obj.addon_versions.items()
        if obj.is_version_blocked(version)
    ])


def block_activity_log_save(obj, change, submission_obj=None):
    action = (
        amo.LOG.BLOCKLIST_BLOCK_EDITED if change else
        amo.LOG.BLOCKLIST_BLOCK_ADDED)
    details = {
        'guid': obj.guid,
        'min_version': obj.min_version,
        'max_version': obj.max_version,
        'url': obj.url,
        'reason': obj.reason,
        'include_in_legacy': obj.include_in_legacy,
        'comments': f'Versions {obj.min_version} - {obj.max_version} blocked.',
    }
    if submission_obj:
        details['signoff_state'] = submission_obj.SIGNOFF_STATES.get(
            submission_obj.signoff_state)
        if submission_obj.signoff_by:
            details['signoff_by'] = submission_obj.signoff_by.id
    al = log_create(
        action, obj.addon, obj.guid, obj, details=details, user=obj.updated_by)
    if submission_obj and submission_obj.signoff_by:
        log_create(
            amo.LOG.BLOCKLIST_SIGNOFF,
            obj.addon,
            obj.guid,
            action.action_class,
            obj,
            user=submission_obj.signoff_by)

    add_version_log_for_blocked_versions(obj, al)


def block_activity_log_delete(obj, *, submission_obj=None, delete_user=None):
    assert submission_obj or delete_user
    details = {
        'guid': obj.guid,
        'min_version': obj.min_version,
        'max_version': obj.max_version,
        'url': obj.url,
        'reason': obj.reason,
        'include_in_legacy': obj.include_in_legacy,
        'comments': f'Versions {obj.min_version} - {obj.max_version} blocked.',
    }
    if submission_obj:
        details['signoff_state'] = submission_obj.SIGNOFF_STATES.get(
            submission_obj.signoff_state)
        if submission_obj.signoff_by:
            details['signoff_by'] = submission_obj.signoff_by.id
    args = (
        [amo.LOG.BLOCKLIST_BLOCK_DELETED] +
        ([obj.addon] if obj.addon else []) +
        [obj.guid, obj])
    al = log_create(
        *args,
        details=details,
        user=submission_obj.updated_by if submission_obj else delete_user)
    if obj.addon:
        add_version_log_for_blocked_versions(obj, al)
    if submission_obj and submission_obj.signoff_by:
        args = (
            [amo.LOG.BLOCKLIST_SIGNOFF] +
            ([obj.addon] if obj.addon else []) +
            [obj.guid, amo.LOG.BLOCKLIST_BLOCK_DELETED.action_class, obj])
        log_create(*args, user=submission_obj.signoff_by)


def splitlines(text):
    return [line.strip() for line in str(text or '').splitlines()]


def legacy_publish_blocks(blocks):
    bucket = settings.REMOTE_SETTINGS_WRITER_BUCKET
    server = KintoServer(bucket, KINTO_COLLECTION_LEGACY)
    for block in blocks:
        needs_updating = block.include_in_legacy and block.kinto_id
        needs_creating = block.include_in_legacy and not block.kinto_id
        needs_deleting = block.kinto_id and not block.include_in_legacy

        if needs_updating or needs_creating:
            if block.is_imported_from_kinto_regex:
                log.debug(
                    f'Block [{block.guid}] was imported from a regex guid so '
                    'can\'t be safely updated.  Skipping.')
                continue
            data = {
                'guid': block.guid,
                'details': {
                    'bug': block.url,
                    'why': block.reason,
                    'name': str(block.reason).partition('.')[0],  # required
                },
                'enabled': True,
                'versionRange': [{
                    'severity': 3,  # Always high severity now.
                    'minVersion': block.min_version,
                    'maxVersion': block.max_version,
                }],
            }
            if needs_creating:
                record = server.publish_record(data)
                block.update(kinto_id=record.get('id', ''))
            else:
                server.publish_record(data, block.kinto_id)
        elif needs_deleting:
            if block.is_imported_from_kinto_regex:
                log.debug(
                    f'Block [{block.guid}] was imported from a regex guid so '
                    'can\'t be safely deleted.  Skipping.')
            else:
                server.delete_record(block.kinto_id)
            block.update(kinto_id='')
        # else no existing kinto record and it shouldn't be in legacy so skip
    server.complete_session()


def legacy_delete_blocks(blocks):
    bucket = settings.REMOTE_SETTINGS_WRITER_BUCKET
    server = KintoServer(bucket, KINTO_COLLECTION_LEGACY)
    for block in blocks:
        if block.kinto_id and block.include_in_legacy:
            if block.is_imported_from_kinto_regex:
                log.debug(
                    f'Block [{block.guid}] was imported from a regex guid so '
                    'can\'t be safely deleted.  Skipping.')
            else:
                server.delete_record(block.kinto_id)
            block.update(kinto_id='')
    server.complete_session()


# Started out based on the regexs in the following url but needed some changes:
# https://dxr.mozilla.org/mozilla-central/source/toolkit/mozapps/extensions/Blocklist.jsm  # noqa

# The whole ID should be surrounded by literal ().
# IDs may contain alphanumerics, _, -, {}, @ and a literal '.'
# They may also contain backslashes (needed to escape the {} and dot)
# We filter out backslash escape sequences (like `\w`) separately
IS_MULTIPLE_ID_SUB_REGEX = r"\([\\\w .{}@-]+\)"
# Find regular expressions of the form:
# /^((id1)|(id2)|(id3)|...|(idN))$/
# The outer set of parens enclosing the entire list of IDs is optional.
IS_MULTIPLE_IDS = re.compile(
    # Start with literal ^ then an optional `(``
    r"^\^\(?" +
    # Then at least one ID in parens ().
    IS_MULTIPLE_ID_SUB_REGEX +
    # Followed by any number of IDs in () separated by pipes.
    r"(?:\|" + IS_MULTIPLE_ID_SUB_REGEX + r")*" +
    # Finally, we need to end with a literal sequence )$
    #  (the leading `)` is optional like at the start)
    r"\)?\$$"
)
# Check for a backslash followed by anything other than a literal . or curlies
REGEX_ESCAPE_SEQS = re.compile(r"\\[^.{}]")
# Used to remove the following 3 things:
# leading literal ^(
#    plus an optional (
# any backslash
# trailing literal )$
#    plus an optional ) before the )$
REGEX_REMOVAL_REGEX = re.compile(r"^\^\(\(?|\\|\)\)?\$$")
GUID_SPLIT = re.compile(r"\)\|\(")


def split_regex_to_list(guid_re):
    if not IS_MULTIPLE_IDS.match(guid_re) or REGEX_ESCAPE_SEQS.match(guid_re):
        return
    trimmed = REGEX_REMOVAL_REGEX.sub('', guid_re)
    return GUID_SPLIT.split(trimmed)


def save_guids_to_blocks(guids, submission):
    from .models import Block

    common_args = {
        'min_version': submission.min_version,
        'max_version': submission.max_version,
        'url': submission.url,
        'reason': submission.reason,
        'updated_by': submission.updated_by,
        'include_in_legacy': submission.include_in_legacy,
    }
    modified_datetime = datetime.datetime.now()

    blocks = Block.get_blocks_from_guids(guids)
    Block.preload_addon_versions(blocks)
    for block in blocks:
        change = bool(block.id)
        for field, val in common_args.items():
            setattr(block, field, val)
        if change:
            setattr(block, 'modified', modified_datetime)
        block.save()
        if submission.id:
            block.submission.add(submission)
        block_activity_log_save(
            block,
            change=change,
            submission_obj=submission if submission.id else None)
    return blocks
