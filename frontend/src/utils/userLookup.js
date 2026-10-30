// Small shared helper for looking up a portal user's photo (for the
// Avatar component) by email — used anywhere a user-authored piece of
// content (note, reply, audit entry, etc.) only has an email/name string
// rather than a full user object. `teamMembers` is the array returned by
// GET /api/meta/options's `team_members` field, which already includes
// each user's `photo_data_url` (see main.py's meta_options()).
export function findUserPhoto(teamMembers, email) {
  if (!email || !Array.isArray(teamMembers)) return null
  const match = teamMembers.find(
    t => (t.email || '').toLowerCase() === email.toLowerCase()
  )
  return match?.photo_data_url || null
}

// Fallback for sites that only have a free-text display name and no email
// (e.g. AWS Support case communications, which come from AWS's own API and
// may not even be a portal user) — best-effort match by exact name.
export function findUserPhotoByName(teamMembers, name) {
  if (!name || !Array.isArray(teamMembers)) return null
  const match = teamMembers.find(
    t => (t.name || '').toLowerCase() === name.toLowerCase()
  )
  return match?.photo_data_url || null
}
