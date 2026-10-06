export function clientMeetingErrors({ meetingUrl = '', participantName = '', objective = '' } = {}) {
  const errors = {};
  const url = String(meetingUrl || '').trim();
  const name = String(participantName || '').trim();
  if (!url) errors.meetingUrl = 'Paste a Zoom, Teams, or Google Meet invitation.';
  else {
    try {
      if (new URL(url).protocol !== 'https:') {
        errors.meetingUrl = 'Use a full https:// Zoom, Teams, or Google Meet invitation.';
      }
    } catch {
      errors.meetingUrl = 'Paste a full https:// invitation URL.';
    }
  }
  if (!String(objective || '').trim()) errors.objective = 'Say what this meeting should achieve.';
  if (!name) errors.participantName = 'Enter the name that should appear in the meeting.';
  return errors;
}

export function createStartLock() {
  let inFlight = false;
  return {
    begin() {
      if (inFlight) return false;
      inFlight = true;
      return true;
    },
    end() {
      inFlight = false;
    },
    get inFlight() {
      return inFlight;
    },
  };
}
