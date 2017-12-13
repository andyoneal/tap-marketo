import csv
import json
import pendulum
import tempfile

import singer
from singer import metadata
from singer import bookmarks

from tap_marketo.client import ExportFailed

# We can request up to 30 days worth of activities per export.
MAX_EXPORT_DAYS = 30

BASE_ACTIVITY_FIELDS = [
    "marketoGUID",
    "leadId",
    "activityDate",
    "activityTypeId",
]

ACTIVITY_FIELDS = BASE_ACTIVITY_FIELDS + [
    "primaryAttributeValue",
    "primaryAttributeValueId",
    "attributes",
]

NO_ASSET_MSG = "No assets found for the given search criteria."
NO_CORONA_WARNING = (
    "Your account does not have Corona support enabled. Without Corona, each sync of "
    "the Leads table requires a full export which can lead to lower data freshness. "
    "Please contact Marketo to request Corona support be added to your account."
)
ITER_CHUNK_SIZE = 512

ATTRIBUTION_WINDOW_DAYS = 1


def format_value(value, schema):
    if not isinstance(schema["type"], list):
        field_type = [schema["type"]]
    else:
        field_type = schema["type"]

    if value in [None, "", 'null']:
        return None
    elif schema.get("format") == "date-time":
        return pendulum.parse(value).isoformat()
    elif "integer" in field_type:
        return int(value)
    elif "string" in field_type:
        return str(value)
    elif "number" in field_type:
        return float(value)
    elif "boolean" in field_type:
        if isinstance(value, bool):
            return value
        return value.lower() == "true"

    return value


def format_values(stream, row):
    rtn = {}
    for field, schema in stream["schema"]["properties"].items():
        if not schema.get("selected") and not (schema.get("inclusion") == "automatic"):
            continue
        rtn[field] = format_value(row.get(field), schema)
    return rtn


def update_state_with_export_info(state, stream, bookmark=None, export_id=None, export_end=None):
    state = bookmarks.write_bookmark(state, stream["tap_stream_id"], "export_id", export_id)
    state = bookmarks.write_bookmark(state, stream["tap_stream_id"], "export_end", export_end)
    if bookmark:
        state = bookmarks.write_bookmark(state, stream["tap_stream_id"], stream["replication_key"], bookmark)

    singer.write_state(state)
    return state


def get_export_end(export_start):
    export_end = export_start.add(days=MAX_EXPORT_DAYS)
    if export_end >= pendulum.utcnow():
        export_end = pendulum.utcnow()

    return export_end.replace(microsecond=0)


def wait_for_export(client, state, stream, export_id):
    stream_type = "activities" if stream["tap_stream_id"] != "leads" else "leads"
    try:
        client.wait_for_export(stream_type, export_id)
    except ExportFailed:
        state = update_state_with_export_info(state, stream)
        raise

    return state


def stream_rows(client, stream_type, export_id):
    with tempfile.NamedTemporaryFile(mode="w+", encoding="utf8") as csv_file:
        singer.log_info("Download starting.")
        resp = client.stream_export(stream_type, export_id)
        for chunk in resp.iter_content(chunk_size=1024, decode_unicode=True):
            if chunk:
                csv_file.write(chunk)

        singer.log_info("Download completed. Begin streaming rows.")
        csv_file.seek(0)
        reader = csv.reader(csv_file, delimiter=',', quotechar='"')
        headers = next(reader)
        for line in reader:
            yield dict(zip(headers, line))


def get_or_create_export_for_leads(client, state, stream, export_start):
    export_id = bookmarks.get_bookmark(state, "leads", "export_id")
    # check if export is still valid
    if not client.export_available("leads", export_id):
        singer.log_info("Export %s no longer available.", export_id)
        export_id = None

    if export_id is None:
        # Corona mode is required to query by "updatedAt", otherwise a full
        # sync is required using "createdAt".
        query_field = "updatedAt" if client.use_corona else "createdAt"
        export_end = get_export_end(export_start)
        query = {query_field: {"startAt": export_start.isoformat(),
                               "endAt": export_end.isoformat()}}

        # Create the new export and store the id and end date in state.
        # Does not start the export (must POST to the "enqueue" endpoint).
        fields = [f for f, s in stream["schema"]["properties"].items()
                  if s.get("selected") or (s.get("inclusion") == "automatic")]
        export_id = client.create_export("leads", fields, query)
        state = update_state_with_export_info(
            state, stream, export_id=export_id, export_end=export_end.isoformat())
    else:
        export_end = pendulum.parse(bookmarks.get_bookmark(state, "leads", "export_end"))

    return export_id, export_end


def get_or_create_export_for_activities(client, state, stream, export_start):
    export_id = bookmarks.get_bookmark(state, stream["tap_stream_id"], "export_id")
    if not client.export_available("activities", export_id):
        singer.log_info("Export %s no longer available.", export_id)
        export_id = None

    if export_id is None:
        # The activity id is in the top-most breadcrumb of the metatdata
        # Activity ids correspond to activity type id in Marketo.
        # We need the activity type id to build the query.
        activity_metadata = metadata.to_map(stream["metadata"])
        activity_type_id = metadata.get(activity_metadata, (), 'marketo.activity-id')

        # Activities must be queried by `createdAt` even though
        # that is not a real field. `createdAt` proxies `activityDate`.
        # The activity type id must also be included in the query. The
        # largest date range that can be used for activities is 30 days.
        export_end = get_export_end(export_start)
        query = {"createdAt": {"startAt": export_start.isoformat(),
                               "endAt": export_end.isoformat()},
                 "activityTypeIds": [activity_type_id]}

        # Create the new export and store the id and end date in state.
        # Does not start the export (must POST to the "enqueue" endpoint).
        export_id = client.create_export("activities", ACTIVITY_FIELDS, query)
        state = update_state_with_export_info(
            state, stream, export_id=export_id, export_end=export_end.isoformat())
    else:
        export_end = pendulum.parse(bookmarks.get_bookmark(state, stream["tap_stream_id"], "export_end"))

    return export_id, export_end


def flatten_activity(row, stream):
    # Start with the base fields
    rtn = {field: row[field] for field in BASE_ACTIVITY_FIELDS}

    # Add the primary attribute name
    # This name is the human readable name/description of the
    # pimaryAttribute
    mdata = metadata.to_map(stream['metadata'])
    pan_field = metadata.get(mdata, (), 'marketo.primary-attribute-name')
    if pan_field:
        rtn['primary_attribute_name'] = pan_field
        rtn['primary_attribute_value'] = row['primaryAttributeValue']
        rtn['primary_attribute_value_id'] = row['primaryAttributeValueId']

    # Now flatten the attrs json to it's selected columns
    if "attributes" in row:
        attrs = json.loads(row["attributes"])
        for key, value in attrs.items():
            key = key.lower().replace(" ", "_")
            rtn[key] = value

    return rtn


def sync_leads(client, state, stream):
    # http://developers.marketo.com/rest-api/bulk-extract/bulk-lead-extract/
    singer.write_schema("leads", stream["schema"], stream["key_properties"])
    initial_bookmark = pendulum.parse(bookmarks.get_bookmark(state, "leads", stream["replication_key"]))
    export_start = pendulum.parse(bookmarks.get_bookmark(state, "leads", stream["replication_key"]))
    if client.use_corona:
        export_start = export_start.subtract(days=ATTRIBUTION_WINDOW_DAYS)

    job_started = pendulum.utcnow()
    record_count = 0
    max_bookmark = initial_bookmark
    while export_start < job_started:
        export_id, export_end = get_or_create_export_for_leads(client, state, stream, export_start)
        state = wait_for_export(client, state, stream, export_id)
        for row in stream_rows(client, "leads", export_id):
            record = format_values(stream, row)
            record_bookmark = pendulum.parse(record["updatedAt"])

            if client.use_corona:
                max_bookmark = export_end
                singer.write_record("leads", record)
                record_count += 1
            elif record_bookmark >= initial_bookmark:
                max_bookmark = max(max_bookmark, record_bookmark)
                singer.write_record("leads", record)
                record_count += 1

        # Now that one of the exports is finished, update the bookmark
        state = update_state_with_export_info(state, stream, bookmark=max_bookmark.isoformat())
        export_start = export_end

    return state, record_count


def sync_activities(client, state, stream):
    # http://developers.marketo.com/rest-api/bulk-extract/bulk-activity-extract/
    singer.write_schema(stream["tap_stream_id"], stream["schema"], stream["key_properties"])
    export_start = pendulum.parse(bookmarks.get_bookmark(state, stream["tap_stream_id"], stream["replication_key"]))
    job_started = pendulum.utcnow()
    record_count = 0
    while export_start < job_started:
        export_id, export_end = get_or_create_export_for_activities(client, state, stream, export_start)
        state = wait_for_export(client, state, stream, export_id)
        for row in stream_rows(client, "activites", export_id):
            row = flatten_activity(row, stream)
            record = format_value(stream, row)
            singer.write_record(stream["tap_stream_id"], record)
            record_count += 1

        state = update_state_with_export_info(state, stream, bookmark=export_start.isoformat())
        export_start = export_end

    return state, record_count


def sync_programs(client, state, stream):
    # http://developers.marketo.com/rest-api/assets/programs/#by_date_range
    #
    # Programs are queryable via their updatedAt time but require and
    # end date as well. As there is no max time range for the query,
    # query from the bookmark value until current.
    #
    # The Programs endpoint uses offsets with a return limit of 200
    # per page. If requesting past the final program, an error message
    # is returned to indicate that the endpoint has been fully synced.
    singer.write_schema("programs", stream["schema"], stream["key_properties"])
    start_date = bookmarks.get_bookmark(state, "programs", "updatedAt")
    end_date = pendulum.utcnow().isoformat()
    params = {
        "maxReturn": 200,
        "offset": 0,
        "earliestUpdatedAt": start_date,
        "latestUpdatedAt": end_date,
    }
    endpoint = "rest/asset/v1/programs.json"

    record_count = 0
    while True:
        data = client.request("GET", endpoint, endpoint_name="programs", params=params)

        # If the no asset message is in the warnings, we have exhausted
        # the search results and can end the sync.
        if NO_ASSET_MSG in data["warnings"]:
            break

        # Each row just needs the values formatted. If the record is
        # newer than the original start date, stream the record.
        for row in data["result"]:
            record = format_values(stream, row)
            if record["updatedAt"] >= start_date:
                record_count += 1
                singer.write_record("programs", record)

        # Increment the offset by the return limit for the next query.
        params["offset"] += params["maxReturn"]

    # Now that we've finished every page we can update the bookmark to
    # the end of the query.
    state = bookmarks.write_bookmark(state, "programs", "updatedAt", end_date)
    singer.write_state(state)
    return state, record_count


def sync_paginated(client, state, stream):
    # http://developers.marketo.com/rest-api/endpoint-reference/lead-database-endpoint-reference/#!/Campaigns/getCampaignsUsingGET
    # http://developers.marketo.com/rest-api/endpoint-reference/lead-database-endpoint-reference/#!/Static_Lists/getListsUsingGET
    #
    # Campaigns and Static Lists are paginated with a max return of 300
    # items per page. There are no filters that can be used to only
    # return updated records.
    singer.write_schema(stream["tap_stream_id"], stream["schema"], stream["key_properties"])
    start_date = bookmarks.get_bookmark(state, stream["tap_stream_id"], stream["replication_key"])
    params = {"batchSize": 300}
    endpoint = "rest/v1/{}.json".format(stream["tap_stream_id"])

    # Paginated requests use paging tokens for retrieving the next page
    # of results. These tokens are stored in the state for resuming
    # syncs. If a paging token exists in state, use it.
    next_page_token = bookmarks.get_bookmark(state, stream["tap_stream_id"], "next_page_token")
    if next_page_token:
        params["nextPageToken"] = next_page_token

    # Keep querying pages of data until no next page token.
    record_count = 0
    job_started = pendulum.utcnow().isoformat()
    while True:
        data = client.request("GET", endpoint, endpoint_name=stream["tap_stream_id"], params=params)

        # Each row just needs the values formatted. If the record is
        # newer than the original start date, stream the record. Finally,
        # update the bookmark if newer than the existing bookmark.
        for row in data["result"]:
            record = format_values(stream, row)
            if record[stream["replication_key"]] >= start_date:
                record_count += 1
                singer.write_record(stream["tap_stream_id"], record)

        # No next page, results are exhausted.
        if "nextPageToken" not in data:
            break

        # Store the next page token in state and continue.
        params["nextPageToken"] = data["nextPageToken"]
        state = bookmarks.write_bookmark(state, stream["tap_stream_id"], "next_page_token", data["nextPageToken"])
        singer.write_state(state)

    # Once all results are exhausted, unset the next page token bookmark
    # so the subsequent sync starts from the beginning.
    state = bookmarks.write_bookmark(state, stream["tap_stream_id"], "next_page_token", None)
    state = bookmarks.write_bookmark(state, stream["tap_stream_id"], stream["replication_key"], job_started)
    singer.write_state(state)
    return state, record_count


def sync_activity_types(client, state, stream):
    # http://developers.marketo.com/rest-api/lead-database/activities/#describe
    #
    # Activity types aren't even paginated. Grab all the results in one
    # request, format the values, and output them.
    singer.write_schema("activity_types", stream["schema"], stream["key_properties"])
    endpoint = "rest/v1/activities/types.json"
    data = client.request("GET", endpoint, endpoint_name="activity_types")
    record_count = 0
    for row in data["result"]:
        record = format_values(stream, row)
        record_count += 1
        singer.write_record("activity_types", record)

    return state, record_count


def sync(client, catalog, state):
    starting_stream = bookmarks.get_currently_syncing(state)
    if starting_stream:
        singer.log_info("Resuming sync from %s", starting_stream)
    else:
        singer.log_info("Starting sync")

    for stream in catalog["streams"]:
        # Skip unselected streams.
        if not stream["schema"].get("selected"):
            singer.log_info("%s: not selected", stream["tap_stream_id"])
            continue

        # Skip streams that have already be synced when resuming.
        if starting_stream and stream["tap_stream_id"] != starting_stream:
            singer.log_info("%s: already synced", stream["tap_stream_id"])
            continue

        singer.log_info("%s: starting sync", stream["tap_stream_id"])

        # Now that we've started, there's no more "starting stream". Set
        # the current stream to resume on next run.
        starting_stream = None
        state = bookmarks.set_currently_syncing(state, stream["tap_stream_id"])
        singer.write_state(state)

        # Sync stream based on type.
        if stream["tap_stream_id"] == "activity_types":
            state, record_count = sync_activity_types(client, state, stream)
        elif stream["tap_stream_id"] == "leads":
            state, record_count = sync_leads(client, state, stream)
        elif stream["tap_stream_id"].startswith("activities_"):
            state, record_count = sync_activities(client, state, stream)
        elif stream["tap_stream_id"] in ["campaigns", "lists"]:
            state, record_count = sync_paginated(client, state, stream)
        elif stream["tap_stream_id"] == "programs":
            state, record_count = sync_programs(client, state, stream)
        else:
            raise Exception("Stream %s not implemented" % stream["tap_stream_id"])

        # Emit metric for record count.
        counter = singer.metrics.record_counter(stream["tap_stream_id"])
        counter.value = record_count
        counter._pop()  # pylint: disable=protected-access

        # Unset current stream.
        state = bookmarks.set_currently_syncing(state, None)
        singer.write_state(state)
        singer.log_info("%s: finished sync", stream["tap_stream_id"])

    # If Corona is not supported, log a warning near the end of the tap
    # log with instructions on how to get Corona supported.
    singer.log_info("Finished sync.")
    if not client.use_corona:
        singer.log_warning(NO_CORONA_WARNING)
