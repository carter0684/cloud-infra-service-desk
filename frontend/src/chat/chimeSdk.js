import {
  ConsoleLogger,
  LogLevel,
  DefaultDeviceController,
  DefaultMeetingSession,
  MeetingSessionConfiguration,
} from 'amazon-chime-sdk-js'

// job-portal-chat-voice Task 15.3 — a thin adapter that turns THIS
// Portal's own flattened call-store field names (chime_meeting_id/
// chime_media_region/chime_media_placement/caller_attendee_id/
// caller_join_token on the POST /api/calls response, or the
// chime_meeting_id/media_region/media_placement/attendee_id/join_token
// on POST /api/calls/{id}/accept's `_join_info`) into the raw
// AWS Chime SDK Meetings API response shape
// ({Meeting: {MeetingId, MediaRegion, MediaPlacement}, Attendee:
// {AttendeeId, JoinToken}}) that amazon-chime-sdk-js's
// MeetingSessionConfiguration constructor actually expects. See
// backend/app/chime_client.py's own docstring for why the backend
// deliberately flattens the raw API shape before ever persisting/
// returning it — this file is where that flattening gets undone again,
// purely because the SDK's constructor has no flattened-shape overload.
//
// Two entry points because the two call-flow branches receive their own
// Chime credentials from two different REST responses with two
// different field-naming conventions (see call_store.py's create_call()
// vs accept_call() — a deliberate design decision, see database.py's
// CallSession model comment):
//   - buildMeetingSessionFromCallResponse(call): the CALLER's own
//     client, immediately after POST /api/calls succeeds.
//   - buildMeetingSessionFromJoinInfo(joinInfo): the CALLEE's own
//     client, immediately after POST /api/calls/{id}/accept succeeds
//     (joinInfo is that response's `_join_info` object) — also reused
//     for a Group_Call participant added via invite_to_active_call()
//     followed by their own accept.

function buildRawMeetingResponse({ meetingId, mediaRegion, mediaPlacement, attendeeId, joinToken }) {
  return {
    Meeting: {
      MeetingId: meetingId,
      MediaRegion: mediaRegion,
      MediaPlacement: mediaPlacement || {},
    },
    Attendee: {
      AttendeeId: attendeeId,
      JoinToken: joinToken,
    },
  }
}

function createMeetingSession(rawMeetingResponse) {
  // 'WARN' rather than the default 'INFO' — amazon-chime-sdk-js is
  // fairly chatty at INFO (every SDP negotiation step, every stats
  // tick), which would flood this app's console far beyond anything
  // else in the codebase; WARN still surfaces anything actually worth
  // noticing (ICE failures, device errors) without the noise.
  const logger = new ConsoleLogger('ChimeSDK', LogLevel.WARN)
  const deviceController = new DefaultDeviceController(logger)
  const configuration = new MeetingSessionConfiguration(
    rawMeetingResponse.Meeting,
    rawMeetingResponse.Attendee,
  )
  return new DefaultMeetingSession(configuration, logger, deviceController)
}

// The CALLER's own client — call immediately after `api.createCall()`
// resolves. `call` is that response body (a call_store.py call dict,
// which top-level carries chime_meeting_id/chime_media_region/
// chime_media_placement/caller_attendee_id/caller_join_token).
export function buildMeetingSessionFromCallResponse(call) {
  return createMeetingSession(
    buildRawMeetingResponse({
      meetingId: call.chime_meeting_id,
      mediaRegion: call.chime_media_region,
      mediaPlacement: call.chime_media_placement,
      attendeeId: call.caller_attendee_id,
      joinToken: call.caller_join_token,
    }),
  )
}

// A CALLEE's (or an invited Group_Call joiner's) own client — call
// immediately after `api.acceptCall()` resolves, passing that
// response's `_join_info` object (chime_meeting_id/media_region/
// media_placement/attendee_id/join_token — call_store.py's
// accept_call() naming, deliberately distinct from the Caller-side
// field names above since they come from a different response/code
// path — see that function's own comment on why).
export function buildMeetingSessionFromJoinInfo(joinInfo) {
  return createMeetingSession(
    buildRawMeetingResponse({
      meetingId: joinInfo.chime_meeting_id,
      mediaRegion: joinInfo.media_region,
      mediaPlacement: joinInfo.media_placement,
      attendeeId: joinInfo.attendee_id,
      joinToken: joinInfo.join_token,
    }),
  )
}
