"""
This script takes the command line input in order to generate a dirty cube for each sideband per correlator (asic and/or swarm) per track,
using a coarse pixel size of 3 arcsec. Each dirty cube is then fed into the findContinuum script, which determines the line-free channels
from an averaged spectrum.
"""

import os
import sys
import glob
import numpy as np
import findContinuum

sourcename = sys.argv[6]
Phase_Center = sys.argv[3] + " " + sys.argv[4] + " " + sys.argv[5]

path = os.getcwd()
os.chdir('./'+sourcename+'_MIR/')

for filename in glob.iglob('*.fits'):
       t = os.path.splitext(filename)
       f = str(t[0])
       if not os.path.exists(f+'.ms'):
           importuvfits(fitsfile=filename,vis=f+'.ms')
       else:
           continue

imagesize=np.loadtxt('/reduction/czdata/dwalker/New/fcont_latest/Image_size_threshold.txt',dtype='str')

for i in range(0,len(imagesize[:]),1):
        if sourcename in imagesize[i][0]:
                ImageSize = [int(imagesize[i][2])/6,int(imagesize[i][2])/6] # Image size here is scaled since the cell size in tclean is smoothed to 3 arcsec, compared to 1 arcsec for the final imaging.

for filename in glob.iglob('*.ms'):
        t = os.path.splitext(filename)
        f = str(t[0])

        if not os.path.exists(f+'.dirty.image'):

            tclean(vis=filename,imagename=f+'.dirty',niter=0,gain=0.1,psfmode='hogbom',imagermode='mosaic',scaletype='SAULT',ftmachine='mosaic',interactive=False,imsize=ImageSize,cell="3arcsec",robust=0.5,weighting='briggs',stokes='I',chaniter=False,mode='frequency',phasecenter=Phase_Center)

        if not os.path.exists(f+'.dirty.image_findContinuum.dat'):

            findContinuum.findContinuum(f+'.dirty.image',sigmaFindContinuum=5.0,singleContinuum=True)
