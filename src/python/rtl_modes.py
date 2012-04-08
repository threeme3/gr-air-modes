#!/usr/bin/env python
# Copyright 2010 Nick Foster
# 
# This file is part of gr-air-modes
# 
# gr-air-modes is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3, or (at your option)
# any later version.
# 
# gr-air-modes is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with gr-air-modes; see the file COPYING.  If not, write to
# the Free Software Foundation, Inc., 51 Franklin Street,
# Boston, MA 02110-1301, USA.
# 

my_position = [37.76225, -122.44254]
#my_position = [37.409066,-122.077836]
#my_position = None

from gnuradio import gr, gru, optfir, eng_notation, blks2, air
from baz import rtl_source_c
from gnuradio.eng_option import eng_option
from optparse import OptionParser
import time, os, sys, threading
from string import split, join
from modes_print import modes_output_print
from modes_sql import modes_output_sql
from modes_sbs1 import modes_output_sbs1
from modes_kml import modes_kml
from modes_raw_server import modes_raw_server
import gnuradio.gr.gr_threading as _threading
import csv

class top_block_runner(_threading.Thread):
    def __init__(self, tb):
        _threading.Thread.__init__(self)
        self.setDaemon(1)
        self.tb = tb
        self.done = False
        self.start()

    def run(self):
        self.tb.run()
        self.done = True

class adsb_rx_block (gr.top_block):
  def __init__(self, options, args, queue):
    gr.top_block.__init__(self)

    self.options = options
    self.args = args
    rate = int(options.rate)

    if options.filename is None:
      self.u = rtl_source_c()

      #if(options.rx_subdev_spec is None):
      #  options.rx_subdev_spec = ""
      #self.u.set_subdev_spec(options.rx_subdev_spec)
      if not options.antenna is None:
        self.u.set_antenna(options.antenna)

      self.u.set_sample_rate(rate)

      if not(self.tune(options.freq)):
        print "Failed to set initial frequency"

      print "Setting gain to %i" % (options.gain,)
      self.u.set_gain(options.gain)
      print "Gain is %i" % (options.gain,)

      self.u.set_verbose(0)
    else:
      self.u = gr.file_source(gr.sizeof_gr_complex, options.filename)

    print "Rate is %i" % (rate,)

    pass_all = 0
    if options.output_all :
      pass_all = 1

    self.demod = gr.complex_to_mag()
    self.avg = gr.moving_average_ff(100, 1.0/100, 400)
    
    #the DBSRX especially tends to be spur-prone; the LPF keeps out the
    #spur multiple that shows up at 2MHz
#    self.lpfiltcoeffs = gr.firdes.low_pass(1, rate, 0.9*rate/2, 50e3)
#    self.lpfiltcoeffs = gr.firdes.low_pass(1, rate, 0.9*rate/2, 100e3)
    self.lpfiltcoeffs = gr.firdes.high_pass(1, rate, 1e3, 100e3)
    self.lpfilter = gr.fir_filter_ccf(1, self.lpfiltcoeffs)
    
    self.preamble = air.modes_preamble(rate, options.threshold)
    #self.framer = air.modes_framer(rate)
    self.slicer = air.modes_slicer(rate, queue)
    
    self.connect(self.u, self.lpfilter, self.demod)
    self.connect(self.demod, self.avg)
    self.connect(self.demod, (self.preamble, 0))
    self.connect(self.avg, (self.preamble, 1))
    self.connect((self.preamble, 0), (self.slicer, 0))

  def tune(self, freq):
    result = self.u.set_frequency(freq)
    return result

def printraw(msg):
    print msg

if __name__ == '__main__':
  usage = "%prog: [options] output filename"
  parser = OptionParser(option_class=eng_option, usage=usage)
  parser.add_option("-R", "--rx-subdev-spec", type="string",
          help="select USRP Rx side A or B", metavar="SUBDEV")
  parser.add_option("-A", "--antenna", type="string",
          help="select which antenna to use on daughterboard")
  parser.add_option("-f", "--freq", type="eng_float", default=1090e6,
                      help="set receive frequency in Hz [default=%default]", metavar="FREQ")
  parser.add_option("-g", "--gain", type="int", default=30,
                      help="set RF gain", metavar="dB")
  parser.add_option("-r", "--rate", type="eng_float", default=2000000,
                      help="set ADC sample rate [default=%default]")
  parser.add_option("-T", "--threshold", type="eng_float", default=0.0,
                      help="set pulse detection threshold above noise in dB [default=%default]")
  parser.add_option("-a","--output-all", action="store_true", default=False,
                      help="output all frames")
  parser.add_option("-F","--filename", type="string", default=None,
            help="read data from file instead of RTL-SDR")
  parser.add_option("-K","--kml", type="string", default=None,
                      help="filename for Google Earth KML output")
  parser.add_option("-P","--sbs1", action="store_true", default=False,
                      help="open an SBS-1-compatible server on port 30003")
  parser.add_option("-w","--raw", action="store_true", default=False,
                      help="open a server outputting raw timestamped data on port 9988")
  parser.add_option("-n","--no-print", action="store_true", default=False,
                      help="disable printing decoded packets to stdout")
  parser.add_option("-l","--location", type="string", default=None,
                      help="GPS coordinates of receiving station in format xx.xxxxx,xx.xxxxx")
  (options, args) = parser.parse_args()

  if options.location is not None:
    reader = csv.reader([options.location], quoting=csv.QUOTE_NONNUMERIC)
    my_position = reader.next()

  queue = gr.msg_queue()
  
  outputs = [] #registry of plugin output functions
  updates = [] #registry of plugin update functions

  if options.kml is not None:
    sqlport = modes_output_sql(my_position, 'adsb.db') #create a SQL parser to push stuff into SQLite
    outputs.append(sqlport.insert)
    #also we spawn a thread to run every 30 seconds (or whatever) to generate KML
    kmlgen = modes_kml('adsb.db', options.kml, my_position) #create a KML generating thread which reads the database

  if options.sbs1 is True:
    sbs1port = modes_output_sbs1(my_position)
    outputs.append(sbs1port.output)
    updates.append(sbs1port.add_pending_conns)
    
  if options.no_print is not True:
    outputs.append(modes_output_print(my_position).parse)

  if options.raw is True:
    rawport = modes_raw_server()
    outputs.append(rawport.output)
    outputs.append(printraw)
    updates.append(rawport.add_pending_conns)

  fg = adsb_rx_block(options, args, queue)
  runner = top_block_runner(fg)

  while 1:
    try:
      #the update registry is really for the SBS1 and raw server plugins -- we're looking for new TCP connections.
      #i think we have to do this here rather than in the output handler because otherwise connections will stack up
      #until the next output arrives
      for update in updates:
        update()
      
      #main message handler
      if queue.empty_p() == 0 :
        while queue.empty_p() == 0 :
          msg = queue.delete_head() #blocking read

          for out in outputs:
            out(msg.to_string())

      elif runner.done:
        raise KeyboardInterrupt
      else:
        time.sleep(0.1)

    except KeyboardInterrupt:
      fg.stop()
      runner = None
      if options.kml is not None:
          kmlgen.done = True
      break
