"""
MIDI UTIL
A small simple and dumb library for parsing and writing midi files.
Parsing will return the midi data as a dict, containing tracks, which are lists of events.

MIDI spec docs:
    * https://www.cs.cmu.edu/~music/cmsip/readings/Standard-MIDI-file-format-updated.pdf
    * http://midi.teragonaudio.com/tech/midispec.htm
"""


# define what should be exported
__all__ = ["decode_midi", "open_midi", "encode_midi", "save_midi", "convert_to_absolute_time", "convert_to_relative_time"]

# constants
PREC_CHANNEL = 9
TAG_HEADER = b"MThd"
TAG_TRACK  = b"MTrk"
READLEN_TABLE = { # for midi events
  0xa0: 2,
  0xb0: 2,
  0xd0: 1,
  0xe0: 2,
}



# basic encoding and decoding of numbers:

def enc_16(x):
  return bytes([x // 256, x % 256])

def dec_16(bd):
  x_256, x_1 = bd.read(2)
  return 256*x_256 + x_1

def enc_32(x):
  ans = []
  for i in range(0, 4):
    ans.append(x % 256)
    x //= 256
  ans.reverse()
  return bytes(ans)

def dec_32(bd):
  ans = 0
  for x_i in bd.read(4):
    ans *= 256
    ans += x_i
  return ans

def enc_vl(x): # variable length encoding
  ans = [x % 128]
  x //= 128
  while x > 0:
    ans.append(128 | (x % 128))
    x //= 128
  ans.reverse()
  return bytes(ans)

def dec_vl(bd): # variable length decoding
  b = bd.read(1)[0]
  ans = b % 128
  while b & 128 != 0:
    b = bd.read(1)[0]
    ans *= 128
    ans += b % 128
  return ans


# MIDI Decoding:

class ByteData:
  def __init__(self, byte_str):
    self.byte_str = byte_str
    self.pos = 0
  def read(self, n):
    base = self.pos
    self.pos += n
    assert self.pos <= len(self.byte_str), "oversized read or unexpected EOS"
    return self.byte_str[base:self.pos]
  def remaining(self):
    return len(self.byte_str) - self.pos
  def dec(self):
    self.pos -= 1
    assert self.pos >= 0, "backed up before start of byte data"

class RunningStatus:
  def __init__(self):
    self.rs = None
  def get(self):
    assert self.rs is not None, "tried to use running status while not set"
    return self.rs
  def set(self, status):
    self.rs = status
  def reset(self):
    self.rs = None

def get_ticks_per_beat(division_code):
  smpte_format = (division_code & 0x8000 != 0)
  if smpte_format:
    negative_smpte_format = (division_code & 0x7f00) >> 8
    ticks_per_frame = division_code & 0x00ff
    assert False, "SMPTE format not yet supported"
  else:
    return division_code & 0x7fff

def dec_meta_event(bd):
  subtype = bd.read(1)[0]
  if 0x01 <= subtype <= 0x0f:
    # text
    length = dec_vl(bd)
    return {
      "subtype": "text",
      "code": subtype,
      "text": bd.read(length),
    }
  elif subtype == 0x2f:
    # end of track
    assert bd.read(1)[0] == 0x00
    assert bd.remaining() == 0, "reached end of track marker before end of track data"
    return {
      "subtype": "end_track",
    }
  elif subtype == 0x51:
    # tempo change
    assert bd.read(1)[0] == 0x03
    tempo_256 = dec_16(bd) # upper 2 bytes of a 3 byte value
    tempo_1 = bd.read(1)[0]
    tempo = 256*tempo_256 + tempo_1
    return {
      "subtype": "tempo",
      "tempo": tempo # in microseconds per beat
    }
  else:
    length = dec_vl(bd)
    return {
      "subtype": "meta_code",
      "code": subtype,
      "data": bd.read(length)
    }

def dec_midi_event(status, bd):
  ans = {"chan": status & 0x0f}
  if status & 0xf0 == 0xc0:
    # change program (i.e. set the instrument)
    ans["type"] = "setinst"
    ans["inst"] = bd.read(1)[0]
  elif status & 0xf0 == 0x80:
    # note off
    ans["type"] = "note_off"
    ans["note"] = bd.read(1)[0]
    ans["velocity"] = bd.read(1)[0] # note-off also has velocity, for some instruments
  elif status & 0xf0 == 0x90:
    # note on
    ans["note"] = bd.read(1)[0]
    velocity = bd.read(1)[0]
    if velocity == 0: # zero velocity corresponds to note-off
      ans["type"] = "note_off"
      ans["velocity"] = 64 # default velocity for note-off is 64
    else:
      ans["type"] = "note_on"
      ans["velocity"] = velocity
  else:
    ans["type"] = "midi_code"
    ans["status"] = status
    ans["data"] = bd.read(READLEN_TABLE[status & 0xf0])
  return ans

def dec_event(bd, running_status):
  delta_t = dec_vl(bd)
  ans = {"dt": delta_t}
  status = bd.read(1)[0]
  if 0xf0 <= status <= 0xf7:
    # system event
    length = dec_vl(bd)
    ans["type"] = "system"
    ans["status"] = status
    ans["data"] = bd.read(length)
    running_status.reset() # cancel the current running status
  elif status == 0xff:
    # meta event
    ans["type"] = "meta"
    ans.update(dec_meta_event(bd))
  elif 0x80 <= status <= 0xef:
    # midi event
    running_status.set(status)
    ans.update(dec_midi_event(status, bd))
  elif status < 0x80:
    # midi event using the running status
    status = running_status.get()
    bd.dec() # move back one so we can reread the data byte as data
    ans.update(dec_midi_event(status, bd))
  else:
    assert False, "unknown event type 0x%x" % status
  return ans

def dec_events(bd):
  ans = []
  running_status = RunningStatus()
  while bd.remaining() > 0:
    ans.append(dec_event(bd, running_status))
  return ans

def dec_chunk(bd):
  chunktype_tag = bd.read(4)
  length = dec_32(bd)
  content = ByteData(bd.read(length))
  if chunktype_tag == TAG_HEADER:
    ans = {"type": "header"}
    ans["format"] = dec_16(content)
    ans["n_tracks"] = dec_16(content)
    ans["ticks_per_beat"] = get_ticks_per_beat(dec_16(content))
  elif chunktype_tag == TAG_TRACK:
    ans = {"type": "track"}
    ans["events"] = dec_events(content)
  else:
    ans = {"type": "unknown"}
  return ans

def dec_chunks(bd):
  ans = []
  while bd.remaining() > 0:
    ans.append(dec_chunk(bd))
  return ans

def decode_midi(midi_bytes):
  """ Given a bytestring, return the result of parsing that string as midi data. """
  assert type(midi_bytes) == bytes
  bd = ByteData(midi_bytes)
  chunks = dec_chunks(bd)
  header_chunk = chunks[0]
  assert header_chunk["type"] == "header", "header chunk is missing"
  tracks = [chunk for chunk in chunks if chunk["type"] == "track"]
  assert header_chunk["n_tracks"] == len(tracks), "wrong number of tracks"
  return {
    "format": header_chunk["format"],
    "ticks_per_beat": header_chunk["ticks_per_beat"],
    "tracks": [track["events"] for track in tracks], # tracks are just lists of events
  }

def open_midi(fnm):
  """ Open a file and return the decoded midi content. """
  with open(fnm, "rb") as f:
    midi_bytes = f.read()
  return decode_midi(midi_bytes)



# MIDI Encoding:

def enc_event(event):
  dt = enc_vl(event["dt"])
  if event["type"] == "note_off":
    return dt + bytes([0x80 + event["chan"], event["note"], event["velocity"]])
  elif event["type"] == "note_on":
    return dt + bytes([0x90 + event["chan"], event["note"], event["velocity"]])
  elif event["type"] == "setinst":
    return dt + bytes([0xC0 + event["chan"], event["inst"]])
  elif event["type"] == "midi_code":
    return dt + bytes([event["status"]]) + event["data"]
  elif event["type"] == "system":
    return dt + bytes([event["status"]]) + enc_vl(len(event["data"])) + event["data"]
  elif event["type"] == "meta":
    if event["subtype"] == "end_track":
      return dt + b"\xff\x2f\x00"
    elif event["subtype"] == "tempo":
      tempo_256 = event["tempo"] // 256
      tempo_1 = event["tempo"] % 256
      return dt + b"\xff\x51\x03" + enc_16(tempo_256) + bytes([tempo_1])
    elif event["subtype"] == "meta_code":
      return dt + bytes([0xff, event["code"]]) + enc_vl(len(event["data"])) + event["data"]
    elif event["subtype"] == "text":
      return dt + bytes([0xff, event["code"]]) + enc_vl(len(event["text"])) + event["text"]
    else:
      assert False, "Can't parse meta event for encoding: %s" % str(event)
  else:
    assert False, "Can't parse event for encoding: %s" % str(event)

def header_chunk(formt, ntrks, division):
  contents = enc_16(formt) + enc_16(ntrks) + enc_16(division)
  chunklen = enc_32(len(contents))
  return TAG_HEADER + chunklen + contents

def track_chunk(events):
  tag = list(TAG_TRACK)
  encoded_events = [enc_event(event) for event in events]
  contents = b"".join(encoded_events)
  return TAG_TRACK + enc_32(len(contents)) + contents

def encode_midi(mididata):
  """ Given midi data in the proper form, convert it to a list of bytes. """
  chunks = [header_chunk(mididata["format"], len(mididata["tracks"]), mididata["ticks_per_beat"])]
  for track in mididata["tracks"]:
    chunks.append(track_chunk(track))
  return b"".join(chunks)

def save_midi(fnm, mididata):
  """ Given midi data in the proper form, save it to a file. """
  midi_bytes = encode_midi(mididata)
  with open(fnm, "wb") as f:
    f.write(midi_bytes)



# Absolute time conversions:

def is_tempo_event(event):
  return ("subtype" in event and event["subtype"] == "tempo")

def get_initial_tempo(mididata):
  """ Get the starting tempo in microseconds per beat. """
  for event in mididata["tracks"][0]:
    if is_tempo_event(event):
      return event["tempo"] # [us/b]
  return 500000 # [us/b] default tempo is 120 bpm

def convert_to_absolute_ticks(track):
  t = 0
  for event in track:
    t += event["dt"]
    del event["dt"]
    event["t"] = t

def convert_to_absolute_time(mididata):
  """ Given a decoded dictionary of midi data, convert all events so that
      time is measured by a "t" in microseconds rather than a "dt" in ticks.
      Note that this function mutates mididata in place!
      Tempo changes are what make this tricky, note that in MIDI a tempo
      change event from any track affects all tracks. """
  # first convert all events to absolute time and gather them
  tracks = mididata["tracks"]
  for track in tracks:
    convert_to_absolute_ticks(track)
  all_events = []
  for track in tracks:
    all_events.extend(track)
  all_events.sort(key=(lambda event: event["t"]))
  # then convert to microseconds
  tempo = get_initial_tempo(mididata) # [us/b]
  tpb = mididata["ticks_per_beat"] # [tk/b]
  t_ticks    = 0 # [tk] time in midi ticks
  t_tc_ticks = 0 # [tk] time of last tempo change, in ticks
  t_tc_us    = 0 # [us] time of last tempo change, in microseconds
  for event in all_events:
    # timekeeping
    t_ticks = event["t"]
    ticks_since_tc = t_ticks - t_tc_ticks # [tk] time since last tempo change
    us_since_tc = (ticks_since_tc*tempo)//tpb # [us] integer division will be as exact as possible
    t_us = t_tc_us + us_since_tc # [us] time in microseconds
    # update the event
    event["t"] = t_us
    # handle tempo changes
    if is_tempo_event(event):
      tempo = event["tempo"]
      t_tc_ticks = t_ticks
      t_tc_us = t_us # potential for small error if above integer divison was not exact
  return mididata

def convert_to_relative_time(mididata):
  """ Converts midi data from absolute time (measured in us) back to relative time
      (measured in ticks). Ticks per beat is pulled from mididata, tempo will be
      the initial tempo specified in the first tempo event. Tempo changes are not
      a thing here, tempo remains constant and note timings are placed on a best-
      effort basis. All tempo events are stripped except for an initial tempo
      event in track 0. """
  tempo = get_initial_tempo(mididata)
  tpb = mididata["ticks_per_beat"]
  # strip tempo events
  mididata["tracks"] = [
      [event for event in track if not is_tempo_event(event)]
    for track in mididata["tracks"]]
  # reinsert initial tempo event
  initial_tempo_event = {
    "t": 0,
    "type": "meta",
    "subtype": "tempo",
    "tempo": tempo,
  }
  mididata["tracks"][0] = [initial_tempo_event] + mididata["tracks"][0]
  # convert to relative time
  for track in mididata["tracks"]:
    t_ticks = 0 # [tk]
    t_prev_event = 0 # [us] the exact time where we placed the previous event
    for event in track:
      new_t_ticks = (event["t"]*tpb)//tempo # [tk]
      dt = new_t_ticks - t_ticks
      t_ticks = new_t_ticks
      del event["t"]
      event["dt"] = dt
  return mididata





