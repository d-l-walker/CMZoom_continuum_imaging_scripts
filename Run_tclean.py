"""
This script takes the command line input in order to generate the cleaned continuum images, via the following:
    - Import MS files for all sidebands per track for the region to be imaged
    - Specify line-free channels using the output from findContinuum and generate continuum-only cubes
    - Spectrally smooth continuum-only cubes in order to speed up imaging
    - Image the continuum with tclean using the smoothed MSs
"""
import os
import sys
import glob
import numpy as np

### Pull sourcename and phasecenter directly from command line input
sourcename = sys.argv[6]
Phase_Center = sys.argv[3] + " " + sys.argv[4] + " " + sys.argv[5]

### Change to source directory
os.chdir('./'+sourcename+'_MIR/')
path = os.getcwd()

### Import CASA MS from MIRIAD UVFITS
#os.system("rm -rf *.ms")
for filename in glob.iglob('*.fits'):
       t = os.path.splitext(filename)
       f = str(t[0])
       if not os.path.exists(f+'.ms'):
           importuvfits(fitsfile=filename,vis=f+'.ms')
       else:
           continue

""" 
Generate averaged line-free, continuum datasets 
(else statement is a relic from before findContinuum had been fully implemented, and will flag a broad frequency range 
that should contain 12CO and 13CO, which are expected to be the dominant lines)
"""
#os.system("rm -rf *.cont.tclean.automultithresh.split2.avg.*")
for filename in glob.iglob('*.ms'):
    if not os.path.exists(sourcename+'.cont.tclean.automultithresh.split2.avg.image'):
        os.system("rm -rf *.flagversions")
        t = os.path.splitext(filename)
        f = str(t[0])
        contvis=f+'.cont.split2.avg.ms'

        if os.path.exists(f+'.dirty.image_findContinuum.dat'):
            continuum_file = np.loadtxt(f+'.dirty.image_findContinuum.dat', dtype=str)[0]
            frange = '0:'+str(continuum_file)
            split2(vis=filename, outputvis=contvis, width=8, datacolumn='data',spw=frange)
        else:
            if 'lsb' in filename:
                        frange='*:220.25~220.45GHz'
            if 'usb' in filename:
                        frange='*:230.45~230.65GHz'
            flagmanager(vis=filename, mode='save', versionname='before_cont_flags')
            initweights(vis=filename, wtmode='weight', dowtsp=True)
            flagchannels = frange
            flagdata(vis=filename, mode='manual', spw=flagchannels, flagbackup=False)
            split2(vis=filename, outputvis=contvis, width=8, datacolumn='data')
            flagmanager(vis=filename,mode='restore',versionname='before_cont_flags')
    else:
        continue

### Round up the relevant files to be imaged
cont_files = []
for filename in glob.iglob('*.cont.split2.avg.ms'):
       if 'cont' in filename:
               cont_files.append(filename)

### tclean doesn't seem to like having more than 10 MSs in the vis parameter. Any regions with > 10 MSs are therefore concatenated.
if len(cont_files) > 10:
    concat(vis=cont_files,concatvis=sourcename+'.concat.all.ms')
    cont_files=sourcename+'.concat.all.ms'

### Pull corresponding image size for the given region to be imaged from the specified text file, along with cleaning threshold.
imagesize=np.loadtxt('/reduction/czdata/dwalker/New/fcont_latest/Image_size_threshold.txt',dtype='str')
for i in range(0,len(imagesize[:]),1):
        if sourcename in imagesize[i][0]:
                ImageSize = [int(imagesize[i][2]),int(imagesize[i][2])]
		Thresh = imagesize[i][4]

### If not already done, perform a full clean of the continuum for the given region
if not os.path.exists(sourcename+'.cont.tclean.automultithresh.newthreshold.split2.avg.image'):
    tclean(vis = cont_files,
           imagename = sourcename+'.cont.tclean.automultithresh.newthreshold.split2.avg',
           phasecenter = Phase_Center,
           specmode = 'mfs',
           deconvolver = 'hogbom',
           scales = [0,3,9,27],
           imsize = ImageSize,
           cell = '0.5arcsec',
           weighting = 'briggs',
           robust = 0.5,
           niter = 1000000,
           threshold = Thresh+'mJy',
           interactive = False,
           gridder = 'mosaic',
           usemask            =  "auto-multithresh",
           sidelobethreshold  =  1.25,
           noisethreshold     =  3.0,
           lownoisethreshold  =  2.0,
           minbeamfrac        =  0.1,
           growiterations     =  75,
           negativethreshold  =  0.0,
           restart            =  True,
           savemodel          =  "none",
           pbcor = True)
else:
    print('Woah! This region has been cleaned. It would be wasteful to do it again. Moving on to the next source ...')
