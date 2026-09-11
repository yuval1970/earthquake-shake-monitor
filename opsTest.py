import numpy as np
import sys, obspy, os
import matplotlib.pyplot as plt

from obspy.core import UTCDateTime
from obspy.clients.fdsn import Client
from obspy.geodetics import gps2dist_azimuth as gps2DistAzimuth # depends on obspy version; this is for v1.1.0
#from obspy.core.util import gps2DistAzimuth

#from PIL import Image
import requests
from io import BytesIO

%matplotlib inline
import warnings
warnings.filterwarnings('ignore')