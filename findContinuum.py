import os
import matplotlib as mpl
if os.environ.get('DISPLAY','') == '':
    mpl.use('Agg')
"""
Demonstration of algorithm to determine continuum channel ranges to 
use from a CASA image cube (dirty or clean).  All dependencies on 
analysisUtils functions have been pasted into this file for convenience.
This function is meant to be run inside CASA.  Simple usage:
  import findContinuum as fc
  fc.findContinuum('my_dirty_cube.image')
This file can be found in a typical pipeline distribution directory, e.g.:
/lustre/naasc/sciops/comm/rindebet/pipeline/branches/trunk/pipeline/extern
"""

import os
try:
    # pyfits is only needed to support reading spectra from FITS tables, 
    # which is not a use case exercised by the pipeline.
    import pyfits
except:
    print 'WARNING: pyfits not available!'

import numpy as np
import matplotlib.pyplot as pl
import matplotlib.ticker
#matplotlib.use('Agg')
import time as timeUtilities
from taskinit import *
from imhead_cli import imhead_cli as imhead
from imregrid_cli import imregrid_cli as imregrid
from imstat_cli import imstat_cli as imstat  # used by computeMadSpectrum
import warnings
import subprocess
import casadef  # This still works in CASA 5.0, but might not someday.
import scipy
import glob
from scipy.stats import scoreatpercentile, percentileofscore
from scipy.ndimage.filters import gaussian_filter

# The following still works in CASA 5 and is backward compatible
# to CASA 4:
casaMajorVersion = int(casadef.casa_version.split('.')[0])
if casaMajorVersion < 5:
    from scipy.stats import nanmean as scipy_nanmean
else:
    # scipy.nanmean exists, but is deprecated in favor of numpy's version
    from numpy import nanmean as scipy_nanmean

maxTrimDefault = 20
_proc_status = '/proc/%d/status' % os.getpid()
_scale = {'kB': 1024.0, 'mB': 1024.0*1024.0,
          'KB': 1024.0, 'MB': 1024.0*1024.0}

def version(showfile=True):
    """
    Returns the CVS revision number.
    """
    myversion = "$Id: findContinuum.py,v 1.167 2017/08/04 12:40:52 thunter Exp $" 
    if (showfile):
        print "Loaded from %s" % (__file__)
    return myversion

def _VmB(VmKey):
    global _proc_status, _scale
     # get pseudo file  /proc/<pid>/status
    try:
        t = open(_proc_status)
        v = t.read()
        t.close()
    except:
        return 0.0  # non-Linux?
     # get VmKey line e.g. 'VmRSS:  9999  kB\n ...'
    i = v.index(VmKey)
    v = v[i:].split(None, 3)  # whitespace
    if len(v) < 3:
        return 0.0  # invalid format?
     # convert Vm value to bytes
    return float(v[1]) * _scale[v[2]]

def memoryUsage(since=0.0):
    '''Return memory usage in gigabytes.
    '''
    return (_VmB('VmSize:') - since)/(1024.**3)

def residentMemoryUsage(since=0.0):
    '''Return resident memory usage in gigabytes.
    '''
    return (_VmB('VmRSS:') - since)/(1024.**3)

def casalogPost(mystring, debug=True):
    if (debug): print mystring
    token = version(False).split()
    origin = token[1].replace(',','_') + token[2]
    casalog.post(mystring.replace('\n',''), origin=origin)
    
def is_binary(filename):
    """
    Return true if the given filename appears to be binary.
    File is considered to be binary if it contains a NULL byte.
    This approach returns True for .fits files, but
    incorrectly reports UTF-16 as binary.
    """
    with open(filename, 'rb') as f:
        for block in f:
            if '\0' in block:
                return True
    return False

def getMemorySize():
    try:
        return(os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES'))
    except ValueError:
        # SC_PHYS_PAGES can be missing on OS X
        return int(subprocess.check_output(['sysctl', '-n', 'hw.memsize']).strip())

def findContinuum(img='', spw='', transition='', baselineModeA='min', baselineModeB='min',
                  sigmaCube=3, nBaselineChannels=0.19, sigmaFindContinuum='auto',
                  verbose=False, png='', pngBasename=False, nanBufferChannels=1, 
                  source='', useAbsoluteValue=True, trimChannels='auto', 
                  percentile=20, continuumThreshold=None, narrow='auto', 
                  separator=';', overwrite=False, titleText='', 
                  showAverageSpectrum=False, maxTrim=maxTrimDefault, maxTrimFraction=1.0,
                  meanSpectrumFile='', centralArcsec='auto', alternateDirectory='.',
                  header='', plotAtmosphere='transmission', airmass=1.5, pwv=1.0,
                  channelFractionForSlopeRemoval=0.75, mask='', 
                  invert=False, meanSpectrumMethod='auto',peakFilterFWHM=10,
                  skyTempThreshold=1.2, # was 1.5 in C4R2, reduced after slope removal added
                  skyTransmissionThreshold=0.08, maxGroupsForSkyThreshold=5,
                  minBandwidthFractionForSkyThreshold=0.2, regressionTest=False,
                  quadraticFit=True, triangleFraction=0.83, maxMemory=-1, 
                  tdmSkyTempThreshold=0.65, negativeThresholdFactor=1.15,
                  vis='', singleContinuum=False, applyMaskToMask=False, 
                  plotBaselinePoints=False, dropBaselineChannels=2.0,
                  madRatioUpperLimit=1.5, madRatioLowerLimit=1.15, interactive=False,
                  projectCode=''):
    """
    This function calls functions to:
    1) compute the mean spectrum of a dirty cube
    2) find the continuum channels 
    3) plot the results
    It calls runFindContinuum up to 2 times.  It runs it a second time with a 
    reduced field size if it finds only one range of continuum channels.

    Returns:
    * A channel selection string suitable for the spw parameter of clean.
    * The name of the final png produced.
    * The aggregate bandwidth in continuum in GHz.

    Inputs:
    img: the image cube to operate upon
    spw: the spw name or number to put in the x-axis label
    transition: the name of the spectral transition (for the plot title)
    baselineModeA: 'min' or 'edge', method to define the baseline in meanSpectrum()
    baselineModeB: 'min' or 'edge', method to define the baseline in findContinuumChannels()
    sigmaCube: multiply this value by the rms to get the threshold above which a pixel
               is included in the mean spectrum
    nBaselineChannels: if integer, then the number of channels to use in:
      a) computing the mean spectrum with the 'meanAboveThreshold' methods.
      b) computing the MAD of the lowest/highest channels in findContinuumChannels
          if float, then the fraction of channels to use (i.e. the percentile)
          default = 0.19, which is 24 channels (i.e. 12 on each side) of a TDM window
    sigmaFindContinuum: passed to findContinuumChannels, 'auto' starts with 3.5
    verbose: if True, then print additional information during processing
    png: the name of the png to produce ('' yields default name)
    pngBasename: if True, then remove the directory from img name before generating png name
    nanBufferChannels: when removing or replacing NaNs, do this many extra channels
                       beyond their extent
    source: the name of the source, to be shown in the title of the spectrum.
            if None, then use the filename, up to the first underscore.
    useAbsoluteValue: passed to meanSpectrum, then avgOverCube -- take absolute value of
             the cube before producing mean spectrum
    findContinuum: if True, then find the continuum channels before plotting
    overwrite: if True, or ASCII file does not exist, then recalculate the mean spectrum
                      writing it to <img>.meanSpectrum
               if False, then read the mean spectrum from the existing ASCII file
    trimChannels: after doing best job of finding continuum, remove this many 
         channels from each edge of each block of channels found (for margin of safety)
         If it is a float between 0..1, then trim this fraction of channels in each 
         group (rounding up). If it is 'auto', use 0.1 but not more than maxTrim channels
         and not more than maxTrimFraction
    percentile: control parameter used with baselineMode='min'
    continuumThreshold: if specified, only use pixels above this intensity level
    separator: the character to use to separate groups of channels in the string returned
    narrow: the minimum number of channels that a group of channels must have to survive
            if 0<narrow<1, then it is interpreted as the fraction of all
                           channels within identified blocks
            if 'auto', then use int(ceil(log10(nchan)))
    titleText: default is img name and transition and the control parameter values
    showAverageSpectrum: make a two-panel plot, showing the raw mean spectrum in black
    maxTrim: in trimChannels='auto', this is the max channels to trim per group for TDM spws; it is automatically scaled upward for FDM spws.
    maxTrimFraction: in trimChannels='auto', the max fraction of channels to trim per group
    meanSpectrumMethod: 'auto', 'peakOverMad', 'peakOverRms', 'meanAboveThreshold', 
       'meanAboveThresholdOverRms', 'meanAboveThresholdOverMad', 
        where 'peak' refers to the peak in a spatially-smoothed version of cube
        'auto' currently invokes 'meanAboveThreshold' unless sky temperature range (max-min)
        from the atmospheric model is more than skyTempThreshold
    skyTempThreshold: rms value in Kelvin, above which meanSpectrumMethod is changed in 'auto' mode to peakOverMad
        for non-TDM spectra. 
    tdmSkyTempThreshold: rms value in Kelvin (for TDM spectra) 
    skyTransmissionThreshold: threshold on (max-min)/mean, above which meanSpectrumMethod is changed in 'auto' mode to peakOverMad
    peakFilterFWHM: value used by 'peakOverRms' and 'peakOverMad' methods to presmooth 
        each plane with a Gaussian kernel of this width (in pixels).  Set to 1 to not do any 
        smoothing.  Default = 10 which is typically about 2 synthesized beams.
    meanSpectrumFile: an alternative ASCII text file to use for the mean spectrum.
                      Must also set img=''.
    centralArcsec: radius of central box within which to compute the mean spectrum
        default='auto' means start with whole field, then reduce to 1/10 if only
        one window is found (unless mask is specified); or 'fwhm' for the central square
        with the same radius as the PB FWHM; or a floating point value in arcseconds
    mask: a mask image preferably equal in shape to the parent image that is used to determine
        the region to compute the noise (outside the mask, i.e. mask=0) and 
        (in the 'meanAboveThresholdMethod') the mean spectrum (inside the mask, i.e. mask=1).  
        The spatial union of all masked pixels in all channels is used as the mask for each channel.
        Option 'auto' will look for the <filename>.mask that matches the <filename>.image
        and if it finds it, it will use it; otherwise it will use no mask.
        If the mask does not match in shape but is multi-channel, it will be regridded to match
        and written out as <filename>.mask.regrid.
        If it matches spatially but is single-channel, this channel will be used for all.
        To convert a .crtf file to a mask, use:
        makemask(mode='copy', inpimage=img, inpmask='chariklo.crtf', output=img+'.mask')

    header: dictionary created by imhead(img, mode='list')
    plotAtmosphere: '', 'tsky', or 'transmission'
    airmass: for plot of atmospheric transmission
    pwv: in mm (for plot of atmospheric transmission)
    channelFractionForSlopeRemoval: if this many channels are initially selected, then fit 
         and remove a linear slope and re-identify continuum channels (for the
         meanAboveThreshold methods only)
    invert: if reading previous meanSpectrum file, then invert the sign and add the minimum
    quadraticFit: if True, fit quadratic polynomial to the noise regions when appropriate; 
         otherwise fit only a linear slope
    triangleFraction: MAD of dual-linfit residual must be less than this fraction*originalMAD
    maxMemory: behave as if the machine has this many GB of memory
    negativeThresholdFactor: scale the nominal negative threshold by this factor (to adjust 
        sensitivity to absorption features: smaller values=more sensitive)
    vis: comma-delimited list or python list of measurement sets to use to convert channel
         ranges to topocentric frequency ranges (for use in uvcontfit or uvcontsub)
    singleContinuum: if True, treat the cube as having come from a Single_Continuum setup;
         For testing purpose. This option is overridden by the contents of vis (if specified).
    applyMaskToMask: if True, apply the mask inside the user mask image to set its masked pixels to 0
    plotBaselinePoints: if True, then plot the baseline-defining points as black dots
    dropBaselineChannels: percentage of extreme values to drop in baseline mode 'min'
    interactive: if True, then stop after first iteration, and wait for input
    """
    if type(centralArcsec) == str:
        if centralArcsec.isdigit():
            centralArcsecValue = centralArcsec
            centralArcsec = float(centralArcsec)
        else:
            centralArcsecValue = "'"+centralArcsec+"'"
    else:
        centralArcsecValue = str(centralArcsec)
    casalogPost("\n BEGINNING: findContinuum.findContinuum('%s', centralArcsec=%s, mask='%s', overwrite=%s, sigmaFindContinuum='%s', meanSpectrumMethod='%s', peakFilterFWHM=%.0f, meanSpectrumFile='%s', triangleFraction=%.2f, singleContinuum=%s)" % (img, centralArcsecValue, mask, overwrite, str(sigmaFindContinuum), meanSpectrumMethod, peakFilterFWHM, meanSpectrumFile, triangleFraction, singleContinuum))
    casalogPost("0) Current memory usage: %.3f GB, resident: %.3f GB" % (memoryUsage(), residentMemoryUsage()))
    img = img.rstrip('/')
    imageInfo = [] # information returned from getImageInfo
    if (len(vis) > 0):
        # vis is a non-blank list or non-blank string
        if (type(vis) == str):
            vis = vis.split(',')
        # vis is now assured to be a non-blank list
        for v in vis:
            if not os.path.exists(v):
                print "Could not find measurement set: ", v
                return
        
    if (img != ''):
        if (not os.path.exists(img)):
            casalogPost("Could not find image = %s" % (img))
            return
        result = getImageInfo(img, header)
        if (result is None): 
            return result
        imageInfo, header = result
        bmaj, bmin, bpa, cdelt1, cdelt2, naxis1, naxis2, freq = imageInfo
        chanInfo = numberOfChannelsInCube(img, returnChannelWidth=True, returnFreqs=True, 
                                          header=header)
        nchan,firstFreq,lastFreq,channelWidth = chanInfo
        if (nchan < 2):
            casalogPost("You must have more than one channel in the image.")
            return
        channelWidth = abs(channelWidth)
    else:
        header = []
        chanInfo = []
        channelWidth = 0
        firstFreq = 0
        lastFreq = 0
    if (mask == 'auto'):
        mask = img.rstrip('.image') + '.mask'
        if (not os.path.exists(mask)):
            mask = ''
        else:
            maskInfo, maskhead = getImageInfo(mask)
            if (maskhead['shape'] == header['shape']).all():
                # are there any unmasked pixels?
                if (maskhead['datamax'] >= 0.5):
                    casalogPost("Will use the clean mask to limit the pixels averaged.")
                    centralArcsec = -1
                else:
                    casalogPost("No valid pixels in the mask.  Will ignore it.")
                    mask = ''
            else:
                casalogPost("Shape of mask (%s) does not match the image (%s)." % (maskhead['shape'],header['shape']))
                casalogPost("If you want to automatically regrid the mask, then set its name explicitly.")
                return
    else:
        if mask == False: 
            mask = ''
        if (len(mask) > 0):
            if (not os.path.exists(mask)):
                casalogPost("Could not find image mask = %s" % (mask))
                return
            maskInfo, maskhead = getImageInfo(mask)
            if (maskhead['shape'] == header['shape']).all():
                casalogPost("Shape of mask matches the image.")
            else:
                casalogPost("Shape of mask (%s) does not match the image (%s)." % (maskhead['shape'],header['shape']))
                # check if the spatial dimension matches.  If so, then simply add channels
                if (maskhead['shape'][0] == header['shape'][0] and
                    maskhead['shape'][1] == header['shape'][1]):
                    myia = createCasaTool(iatool)
                    myia.open(img)
                    axis = findSpectralAxis(myia)
                    if (header['shape'][axis] != 1):
                        if (os.path.exists(mask+'.regrid') and not overwrite):
                            casalogPost("Regridded mask already exists, will use it.")
                        else:
                            casalogPost("Regridding the spectral axis of the mask with replicate.")
                            imregrid(mask, output=mask+'.regrid', template=img, axes=[axis], replicate=True, interpolation='nearest', overwrite=overwrite)
                    else:
                        casalogPost("This single plane mask will be used for all channels.")
                else:
                    casalogPost("Regridding the mask spatially and spectrally.")
                    imregrid(mask, output=mask+'.regrid', template=img, asvelocity=False, interpolation='nearest')
                mask = mask+'.regrid'
                maskInfo, maskhead = getImageInfo(mask)

    bytes = getMemorySize()
    try:
        hostname = os.getenv('HOST')
    except:
        hostname = "?"
    casalogPost("Total memory on %s = %.3f GB" % (hostname,bytes/(1024.**3)))
    if (maxMemory > 0 and maxMemory < bytes/(1024.**3)):
        bytes = maxMemory*(1024.**3)
        casalogPost("but behaving as if it has only %.3f GB" % (bytes/(1024.**3)))
    meanSpectrumMethodRequested = meanSpectrumMethod
    meanSpectrumMethodMessage = ''
    if img != '':
        npixels = float(nchan)*naxis1*naxis2
    else:
        npixels = 1
    maxpixels = bytes/67 # float(1024)*1024*960
    triangularPatternSeen = False
    if (meanSpectrumMethod.find('auto') >= 0):
        meanSpectrumMethod = 'meanAboveThreshold'
        if (img != ''):
            a,b,c,d,e = atmosphereVariation(img, header, chanInfo, 
                                          airmass=airmass, pwv=pwv)
            if (b > skyTransmissionThreshold or e > skyTempThreshold):
                meanSpectrumMethod = 'peakOverMad'
                meanSpectrumMethodMessage = "Set meanSpectrumMethod='%s' since atmos. variation %.2f>%.2f or %.3f>%.1fK." % (meanSpectrumMethod,b,skyTransmissionThreshold,e,skyTempThreshold)
            elif (e > tdmSkyTempThreshold and abs(channelWidth*nchan) > 1e9): # tdmSpectrum(channelWidth,nchan)):
                meanSpectrumMethod = 'peakOverMad'
                meanSpectrumMethodMessage = "Set meanSpectrumMethod='%s' since atmos. variation %.2f>%.2f or %.3f>%.2fK." % (meanSpectrumMethod,b,skyTransmissionThreshold,e,tdmSkyTempThreshold)
            else:
                # Maybe comment this out once thresholds are stable
                if abs(channelWidth*nchan > 1e9): # tdmSpectrum(channelWidth,nchan):
                    myThreshold = tdmSkyTempThreshold
                else:
                    myThreshold = skyTempThreshold
                triangularPatternSeen, value = checkForTriangularWavePattern(img,header,triangleFraction)
                if (triangularPatternSeen):
                    meanSpectrumMethod = 'peakOverMad'
                    meanSpectrumMethodMessage = "Set meanSpectrumMethod='%s' since triangular pattern was seen (%.2f<%.2f)." % (meanSpectrumMethod, value, triangleFraction)
                else:
                    if value == False:
                        triangleMsg = 'slopeTest=F'
                    else:
                        triangleMsg = '%.2f>%.2f' % (value,triangleFraction)
                    meanSpectrumMethodMessage = "Did not change meanSpectrumMethod since atmos. variation %.2f<%.2f & %.3f<%.1fK (%s)." % (b,skyTransmissionThreshold,e,myThreshold,triangleMsg)

            casalogPost(meanSpectrumMethodMessage)

    centralArcsecField = centralArcsec                    
    centralArcsecLimitedField = -1
    if (centralArcsec == 'auto' and img != ''):
        if (npixels > maxpixels): # and meanSpectrumMethod.find('meanAboveThreshold')>=0):
            casalogPost("Excessive number of pixels (%.0f > %.0f) %dx%dx%d" % (npixels,maxpixels,naxis1,naxis2,nchan))
            totalWidthArcsec = abs(cdelt2*naxis2)
            if (mask == ''):
                centralArcsecField = totalWidthArcsec*maxpixels/npixels
            else:
                casalogPost("Finding size of the central square that fully contains the mask.")
                centralArcsecField = widthOfMaskArcsec(mask)
            newWidth = int(naxis2*centralArcsecField/totalWidthArcsec)
            centralArcsecLimitedField = float(centralArcsecField)  # Remember in case we need to reinvoke later
            if (header['maxpixpos'][0] > naxis2/2 - newWidth/2 and 
                header['maxpixpos'][0] < naxis2/2 + newWidth/2 and
                header['maxpixpos'][1] > naxis2/2 - newWidth/2 and 
                header['maxpixpos'][1] < naxis2/2 + newWidth/2):
                npixels = float(nchan)*newWidth*newWidth
                casalogPost('Data max is within the smaller field')
                casalogPost("Reducing image width examined from %.2f to %.2f arcsec to avoid memory problems." % (totalWidthArcsec,centralArcsecField))
            else:
                centralArcsecField = centralArcsec
                if (meanSpectrumMethod == 'peakOverMad'):
                    casalogPost('Data max is NOT within the smaller field. Keeping meanSpectrumMethod of peakOverMad over full field.')
                else:
                    meanSpectrumMethod = 'peakOverMad'
                    meanSpectrumMethodMessage = "Data max NOT within the smaller field. Switch to meanSpectrumMethod='peakOverMad'"
                    casalogPost(meanSpectrumMethodMessage)
        else:
            casalogPost("Using whole field since npixels=%d < maxpixels=%d" % (npixels, maxpixels))
            centralArcsecField = -1  # use the whole field
    elif (centralArcsec == 'fwhm' and img != ''):
        centralArcsecField = primaryBeamArcsec(frequency=np.mean([firstFreq,lastFreq]),
                                               showEquation=False)
        npixels = float(nchan)*(centralArcsecField**2/abs(cdelt2)/abs(cdelt1))
    else:  
        centralArcsecField = centralArcsec
        if img != '':
            npixels = float(nchan)*(centralArcsec**2/abs(cdelt2)/abs(cdelt1))

    iteration = 0
    fullLegend = False
    casalogPost('---------- runFindContinuum iteration %d' % (iteration))
    if vis != '':
        singleContinuum = isSingleContinuum(vis, spw)
    result = runFindContinuum(img, spw, transition, baselineModeA,baselineModeB,
                              sigmaCube, nBaselineChannels, sigmaFindContinuum,
                              verbose, png, pngBasename, nanBufferChannels, 
                              source, useAbsoluteValue, trimChannels, 
                              percentile, continuumThreshold, narrow, 
                              separator, overwrite, titleText, 
                              showAverageSpectrum, maxTrim, maxTrimFraction,
                              meanSpectrumFile, centralArcsecField,channelWidth,
                              alternateDirectory, imageInfo, chanInfo, header,
                              plotAtmosphere, airmass, pwv, 
                              channelFractionForSlopeRemoval, mask, 
                              invert, meanSpectrumMethod, peakFilterFWHM, 
                              fullLegend, iteration, meanSpectrumMethodMessage,
                              regressionTest=regressionTest, quadraticFit=quadraticFit,
                              megapixels=npixels*1e-6, triangularPatternSeen=triangularPatternSeen,
                              maxMemory=maxMemory, negativeThresholdFactor=negativeThresholdFactor,
                              byteLimit=bytes, singleContinuum=singleContinuum,
                              applyMaskToMask=applyMaskToMask, plotBaselinePoints=plotBaselinePoints,
                              dropBaselineChannels=dropBaselineChannels,
                              madRatioUpperLimit=madRatioUpperLimit, 
                              madRatioLowerLimit=madRatioLowerLimit, projectCode=projectCode)
    if result is None:
        return
    selection, mypng, slope, channelWidth, nchan, useLowBaseline = result
    if png == '':
        png = mypng
    mytest = False
    if (centralArcsec == 'auto' and img != '' and len(selection.split(separator)) < 2):
        # Only one range was found, so look closer into the center for a line
        casalogPost("Only one range of channels was found")
        print "Only one range found....."
        myselection = selection.split(separator)[0]
        if (myselection.find('~') > 0):
            a,b = [int(i) for i in myselection.split('~')]
            print "Comparing range of %d~%d to %d/2" % (a,b,nchan)
            mytest = b-a+1 > nchan/2
        else:
            mytest = True
        if (mytest):
            reductionFactor = 10.0
            if (naxis1 > 1*6*reductionFactor): # reduced field must be at least 1 beam across (assuming 6 pix per beam)
                # reduce the field size to one tenth of the previous
                bmaj, bmin, bpa, cdelt1, cdelt2, naxis1, naxis2, freq = imageInfo # getImageInfo(img)
                imageWidthArcsec = 0.5*(np.abs(naxis2*cdelt2) + np.abs(naxis1*cdelt1))
                npixels /= reductionFactor**2
                centralArcsecField = imageWidthArcsec/reductionFactor
                casalogPost("Reducing the field size to 1/10 of previous (%f arcsec to %f arcsec) (%d to %d pixels)" % (imageWidthArcsec,centralArcsecField,naxis1,naxis1/10))
                # could change the 128 to tdmSpectrum(channelWidth,nchan), but this heuristic may also help
                # for excerpts of larger cubes with narrower channel widths.
                if (nBaselineChannels < 1 and nchan <= 128):  
                    nBaselineChannels = float(np.min([0.5, nBaselineChannels*1.5]))
                overwrite = True
                casalogPost("Re-running findContinuum over central %.1f arcsec with nBaselineChannels=%g" % (centralArcsecField,nBaselineChannels))
                iteration += 1
                if interactive:
                    ignore = raw_input("Press return to continue")
                result = runFindContinuum(img, spw, transition, baselineModeA, baselineModeB,
                                          sigmaCube, nBaselineChannels, sigmaFindContinuum,
                                          verbose, png, pngBasename, nanBufferChannels, 
                                          source, useAbsoluteValue, trimChannels, 
                                          percentile, continuumThreshold, narrow, 
                                          separator, overwrite, titleText, 
                                          showAverageSpectrum, maxTrim, maxTrimFraction,
                                          meanSpectrumFile, centralArcsecField, channelWidth,
                                          alternateDirectory, imageInfo, chanInfo, header,
                                          plotAtmosphere, airmass, pwv, 
                                          channelFractionForSlopeRemoval, mask, 
                                          invert, meanSpectrumMethod, peakFilterFWHM, 
                                          fullLegend,iteration,meanSpectrumMethodMessage,
                                          regressionTest=regressionTest, quadraticFit=quadraticFit,
                                          megapixels=npixels*1e-6, triangularPatternSeen=triangularPatternSeen,
                                          maxMemory=maxMemory, negativeThresholdFactor=negativeThresholdFactor,
                                          byteLimit=bytes, singleContinuum=singleContinuum,
                                          applyMaskToMask=applyMaskToMask, plotBaselinePoints=plotBaselinePoints,
                                          dropBaselineChannels=dropBaselineChannels,
                                          madRatioUpperLimit=madRatioUpperLimit, 
                                          madRatioLowerLimit=madRatioLowerLimit, projectCode=projectCode)
                if result is None:
                    return
                selection, mypng, slope, channelWidth, nchan, useLowBaseline = result
                if png == '':
                    png = mypng
                    print "************ b) png passed into initial call was blank"
            else:
                casalogPost("*** Not reducing field size since it would be less than 1 beam across")
    else:
        casalogPost("*** Not reducing field size since more than 1 range found")
    aggregateBandwidth = computeBandwidth(selection, channelWidth, 0)
    if (meanSpectrumMethodRequested == 'auto'):
      # Here we check to see if we need to switch the method of computing the mean spectrum
      # based on any undesirable characteristics of the results, and if so, re-run it.
      groups = len(selection.split(separator))
      if (aggregateBandwidth <= 0.00001 or 
          # the following line was an attempt to solve CAS-9269 case reported by Ilsang Yoon for SCOPS-2571
#          (mytest and meanSpectrumMethod!='peakOverMad' and groups < 2) or
          (not useLowBaseline and meanSpectrumMethod=='peakOverMad' and not tdmSpectrum(channelWidth,nchan)) or
          (meanSpectrumMethod=='peakOverMad' and meanSpectrumMethodMessage != ''
           and groups > maxGroupsForSkyThreshold
           # the following line maintains peakOverMad for dataset in CAS-8908 (and also OMC1_NW spw39 in CAS-8720)
           and aggregateBandwidth < minBandwidthFractionForSkyThreshold*nchan*channelWidth*1e-9
           and not tdmSpectrum(channelWidth,nchan))):
        # If less than 10 kHz is found (or two other situations), then try the other approach.
        if (meanSpectrumMethod == 'peakOverMad'):
            meanSpectrumMethod = 'meanAboveThreshold'
            if (aggregateBandwidth <= 0.00001):
                meanSpectrumMethodMessage = "Reverted to meanSpectrumMethod='%s' because no continuum found." % (meanSpectrumMethod)
                casalogPost("Re-running findContinuum with the other meanSpectrumMethod: %s (because aggregateBW=%eGHz is less than 10kHz)" % (meanSpectrumMethod,aggregateBandwidth))
            elif (not useLowBaseline and meanSpectrumMethod=='peakOverMad' and not tdmSpectrum(channelWidth,nchan)):
                meanSpectrumMethodMessage = "Reverted to meanSpectrumMethod='%s' because useLowBaseline=F." % (meanSpectrumMethod)
                casalogPost("Re-running findContinuum with the other meanSpectrumMethod: %s (because useLowBaseline=False)" % (meanSpectrumMethod))
            elif (aggregateBandwidth < minBandwidthFractionForSkyThreshold*nchan*channelWidth*1e-9 and groups>maxGroupsForSkyThreshold):
                meanSpectrumMethodMessage = "Reverted to meanSpectrumMethod='%s' because groups=%d>%d and not TDM." % (meanSpectrumMethod,groups,maxGroupsForSkyThreshold)
                casalogPost("Re-running findContinuum with the other meanSpectrumMethod: %s because it is an FDM spectrum with many groups (%d>%d) and aggregate bandwidth (%f GHz) < %.2f of total bandwidth." % (meanSpectrumMethod,groups,maxGroupsForSkyThreshold,aggregateBandwidth,minBandwidthFractionForSkyThreshold))
            else:
                if groups < 2:
                    if sigmaFindContinuum == 'auto':
                        # Fix for CAS-9639: strong line near band edge and odd noise characteristic
                        sigmaFindContinuum = 6.5
                        casalogPost('Setting sigmaFindContinuum = %.1f since groups < 2' % sigmaFindContinuum)
                    else:
                        sigmaFindContinuum += 3.0
                    meanSpectrumMethodMessage = "Increasing sigmaFindContinuum to %.1f because groups=%d<2 and not TDM." % (sigmaFindContinuum,groups)
                    casalogPost("Re-running findContinuum with meanSpectrumMethod: %s because groups=%d<2. Increasing sigmaFindContinuum to %.1f." % (meanSpectrumMethod,groups,sigmaFindContinuum))
                elif groups > maxGroupsForSkyThreshold:
                    # hot core FDM case
                    meanSpectrumMethodMessage = "Reverted to meanSpectrumMethod='%s' because groups=%d>%d and not TDM." % (meanSpectrumMethod,groups,maxGroupsForSkyThreshold)
                    casalogPost("Re-running findContinuum with meanSpectrumMethod: %s." % (meanSpectrumMethod))
                else:
                    meanSpectrumMethodMessage = "Reverted to meanSpectrumMethod='%s' because groups=%d and not TDM." % (meanSpectrumMethod,groups)
                    casalogPost("Re-running findContinuum with meanSpectrumMethod: %s." % (meanSpectrumMethod))
            if (centralArcsecLimitedField > 0):
                # Re-establish the previous limit
                centralArcsecField = centralArcsecLimitedField
                casalogPost("Re-establishing the limit on field width determined earlier: %f arcsec" %(centralArcsecField)) 
        else:
            meanSpectrumMethod = 'peakOverMad'
            if (mytest and groups < 2):
                centralArcsecField = -1
                casalogPost("Re-running findContinuum with meanSpectrumMethod: %s (because still only 1 group found after zoom)" % (meanSpectrumMethod))
            else:
                casalogPost("Re-running findContinuum with the other meanSpectrumMethod: %s (because aggregateBW=%eGHz is less than 10kHz)" % (meanSpectrumMethod,aggregateBandwidth))
            
        iteration += 1
        if os.path.exists(png):
            os.remove(png)
        if interactive:
            ignore = raw_input("Press return to continue")
        result = runFindContinuum(img, spw, transition, baselineModeA, baselineModeB,
                                  sigmaCube, nBaselineChannels, sigmaFindContinuum,
                                  verbose, png, pngBasename, nanBufferChannels, 
                                  source, useAbsoluteValue, trimChannels, 
                                  percentile, continuumThreshold, narrow, 
                                  separator, overwrite, titleText, 
                                  showAverageSpectrum, maxTrim, maxTrimFraction,
                                  meanSpectrumFile, centralArcsecField, channelWidth,
                                  alternateDirectory, imageInfo, chanInfo, header,
                                  plotAtmosphere, airmass, pwv, 
                                  channelFractionForSlopeRemoval, mask, 
                                  invert, meanSpectrumMethod, peakFilterFWHM, 
                                  fullLegend, iteration, 
                                  meanSpectrumMethodMessage,
                                  regressionTest=regressionTest, quadraticFit=quadraticFit,
                                  megapixels=npixels*1e-6, triangularPatternSeen=triangularPatternSeen,
                                  maxMemory=maxMemory, negativeThresholdFactor=negativeThresholdFactor,
                                  byteLimit=bytes, singleContinuum=singleContinuum, 
                                  applyMaskToMask=applyMaskToMask, plotBaselinePoints=plotBaselinePoints,
                                  dropBaselineChannels=dropBaselineChannels,
                                  madRatioUpperLimit=madRatioUpperLimit, 
                                  madRatioLowerLimit=madRatioLowerLimit, projectCode=projectCode)

        selection, mypng, slope, channelWidth, nchan, useLowBaseline = result
        if png == '':
            png = mypng
            print "************ c) png passed into initial call was blank"
      else:
          casalogPost("Not re-running findContinuum with the other method because the results are deemed acceptable.")
    # Write summary of results to text file
    if (meanSpectrumFile == ''): 
        meanSpectrumFile = buildMeanSpectrumFilename(img, meanSpectrumMethod, peakFilterFWHM)
    writeContDat(meanSpectrumFile, selection, png, aggregateBandwidth,
                 firstFreq, lastFreq, channelWidth, img, vis)
    casalogPost("Current memory usage: %.3f GB, resident: %.3f GB" % (memoryUsage(), residentMemoryUsage()))
    casalogPost("Finished findContinuum.py.")
    return(selection, png, aggregateBandwidth)

def maskArgumentMismatch(mask, meanSpectrumFile):
    """
    Determines if the requested mask does not match what was used to generate 
    the specified meanSpectrumFile.
    Returns: True or False
    """
    if (mask == '' or mask == False):
        if (grep(meanSpectrumFile,'mask')[0] != ''): 
            # mask was used previously, but we did not request one this time
            return True
        else:
            # mask was not used previously and we are not requesting one this time
            False
    elif (grep(meanSpectrumFile,mask)[0] == ''):
        # requested mask was not used previously
        return True
    else:
        # requested mask was used previously.  Except if the new mask is a subset 
        # of the old name, so check for that possibility.
        tokens = grep(meanSpectrumFile,mask)[0].split()
        if mask not in tokens:
            return True
        else:
            return False

def centralArcsecArgumentMismatch(centralArcsec, meanSpectrumFile, iteration):
    """
    Determines if the requested centralArcsec value does not match what was used to 
    generate the specified meanSpectrumFile.
    Returns: True or False
    """
    if (centralArcsec == 'auto' or centralArcsec == -1):
        if (grep(meanSpectrumFile,'centralArcsec=auto')[0] == '' and 
            grep(meanSpectrumFile,'centralArcsec=-1')[0] == ''):
            casalogPost("request for auto but 'centralArcsec=auto' not found and 'centralArcsec=-1' not found")
            return True
        else:
            result = grep(meanSpectrumFile,'centralArcsec=auto')[0]
            if (iteration == 0 and result != ''):
                # This will force re-run of any result that went to a smaller size.
                token = result.split('=auto')
                if (len(token) > 1):
                    if (token[1].strip().replace('.','').isdigit()):
                        return True
                    else:
                        return False
                else:
                    return False
            else:
                return False
    elif (grep(meanSpectrumFile,'centralArcsec=auto %s'%(str(centralArcsec)))[0] == '' and
          grep(meanSpectrumFile,'centralArcsec=%s'%(str(centralArcsec)))[0] == ''):
        token = grep(meanSpectrumFile,'centralArcsec=')[0].split('centralArcsec=')
        if (len(token) < 2):
            # This should never happen, but if it does, prevent a crash by returning now.
            casalogPost("Did not find string 'centralArcsec=' with a value in the meanSpectrum file.")
            return True
        value = token[1].replace('auto ','').split()[0]
        try:
            previousRequest = float(value)
            centralArcsecThresholdPercent = 2
            if (100*abs(previousRequest-centralArcsec)/centralArcsec < centralArcsecThresholdPercent):
                casalogPost("request for specific value (%s) and previous value (%s) is within %d percent" % (str(centralArcsec),str(previousRequest),centralArcsecThresholdPercent))
                return False
            else:
                casalogPost("request for specific value but 'centralArcsec=auto %s' not within %d percent" % (str(centralArcsec),centralArcsecThresholdPercent))
                return True
        except:
            # If there is any trouble reading the previous value, then return now.
            casalogPost("Failed to parse floating point value.")
            return True
    else:
        return False

def writeContDat(meanSpectrumFile, selection, png, aggregateBandwidth, 
                 firstFreq, lastFreq, channelWidth, img, vis='',
                 numchan=None, chanInfo=None, lsrkwidth=None):
    """
    Writes out an ASCII file called <meanSpectrumFile>_findContinuum.dat
    that contains the selected channels, the png name and the aggregate 
    bandwidth in GHz.
    Returns: None
    """
    if (meanSpectrumFile.find('.meanSpectrum') > 0):
        contDat = meanSpectrumFile.split('.meanSpectrum')[0] + '_findContinuum.dat'
    else:
        contDat = meanSpectrumFile + '_findContinuum.dat'
    contDatDir = os.path.dirname(contDat)
    if (firstFreq > lastFreq and channelWidth > 0):
        # restore negative channel widths if appropriate
        channelWidth *= -1
    lsrkfreqs = 1e-9*np.arange(firstFreq, lastFreq+channelWidth*0.5, channelWidth)
    if (len(contDatDir) < 1):
        contDatDir = '.'
    if (not os.access(contDatDir, os.W_OK) and contDatDir != '.'):
        # Tf there is no write permission, then use the directory of the png
        # since this has been established as writeable in runFindContinuum.
        contDat = os.path.dirname(png) + '/' + os.path.basename(contDat)
#    try:
    if True:
        f = open(contDat, 'w')
        f.write('%s %s %g\n' % (selection, png, aggregateBandwidth))
        if (len(vis) > 0):
            # vis is a non-blank list or non-blank string
            if (type(vis) == str):
                vis = vis.split(',')
            # vis is now assured to be a non-blank list
            for v in vis:
                casalogPost("Converting from LSRK to TOPO for vis = %s" % (v))
                vselection = ''
                for i,s in enumerate(selection.split(';')):
                    c0,c1 = [int(j) for j in s.split('~')]
                    minFreq = np.min([lsrkfreqs[c0],lsrkfreqs[c1]])-0.5*abs(channelWidth*1e-9)
                    maxFreq = np.max([lsrkfreqs[c0],lsrkfreqs[c1]])+0.5*abs(channelWidth*1e-9)
                    # print "Read channels: ", c0, c1
                    freqRange = '%.5fGHz~%.5fGHz' % (minFreq,maxFreq)
                    casalogPost("LSRK freqRange = %s" % str(freqRange))
                    result = cubeLSRKToTopo(img, freqRange, vis=v)*1e-9
                    # pipeline calls uvcontfit with GHz label only on upper freq
                    freqRange = '%.5f~%.5fGHz' % (np.min(result),np.max(result))
                    casalogPost("Topo freqRange = %s" % str(freqRange))
                    if (i > 0): vselection += ';'
                    vselection += freqRange
                f.write('%s %s\n' % (v,vselection))
        f.close()
        casalogPost("Wrote %s" % (contDat))
#    except:
#        casalogPost("Failed to write %s" % (contDat))

def drawYlabel(img, typeOfMeanSpectrum, meanSpectrumMethod, meanSpectrumThreshold,
               peakFilterFWHM, fontsize):
    """
    Draws a descriptive y-axis label based on the origin and type of mean spectrum used.
    Returns: None
    """
    if (img == ''):
        label = 'Mean spectrum passed in as ASCII file'
    else:
        if (meanSpectrumMethod.find('meanAboveThreshold') >= 0):
            if (meanSpectrumMethod.find('OverMad') > 0):
                label = '(Average spectrum > threshold=(%g))/MAD' % (roundFigures(meanSpectrumThreshold,3))
            elif (meanSpectrumMethod.find('OverRms') > 0):
                label = '(Average spectrum > threshold=(%g))/RMS' % (roundFigures(meanSpectrumThreshold,3))
            else:
                label = 'Average spectrum > threshold=(%g)' % (roundFigures(meanSpectrumThreshold,3))
        elif (meanSpectrumMethod.find('peakOverMad')>=0):
            if peakFilterFWHM > 1:
                label = 'Per-channel (Peak / MAD) of image smoothed by FWHM=%d pixels' % (peakFilterFWHM)
            else:
                label = 'Per-channel (Peak / MAD)'
        elif (meanSpectrumMethod.find('peakOverRms')>=0):
            if peakFilterFWHM > 1:
                label = 'Per-channel (Peak / RMS) of image smoothed by FWHM=%d pixels' % (peakFilterFWHM)
            else:
                label = 'Per-channel (Peak / RMS)'
    pl.ylabel(typeOfMeanSpectrum+' '+label, size=fontsize)

def computeBandwidth(selection, channelWidth, loc=-1):
    """
    selection: a string of format:  '5~6;9~20'
    channelWidth: in Hz
    Returns: bandwidth in GHz
    """
#    print "***%d*****  Called computeBandwidth('%s', %s)" % (loc,selection, str(channelWidth))
    ranges = selection.split(';')
    channels = 0
    for r in ranges:
        result = r.split('~')
        if (len(result) == 2):
            a,b = result
            channels += int(b)-int(a)+1
    aggregateBW = channels * abs(channelWidth) * 1e-9
#    print "********** channels = %d, channelWidth=%s, aggregateBW=%g" % (channels,str(channelWidth), aggregateBW)
    return(aggregateBW)

def buildMeanSpectrumFilename(img, meanSpectrumMethod, peakFilterFWHM):
    """
    Creates the name of the meanSpectrumFile to search for and/or create.
    Returns: a string
    """
    if (meanSpectrumMethod.find('peak')>=0):
        return(img + '.meanSpectrum.'+meanSpectrumMethod+'_%d'%peakFilterFWHM)
    else:
        if (meanSpectrumMethod == 'meanAboveThreshold'):
            return(img + '.meanSpectrum')
        else:
            return(img + '.meanSpectrum.'+meanSpectrumMethod)

def tdmSpectrum(channelWidth, nchan):
    """
    Use 15e6 instead of 15.625e6 because LSRK channel width can be slightly narrower than TOPO.
    Returns: True or False
    """
    if ((channelWidth >= 15e6/2. and nchan>240) or # 1 pol TDM, must avoid 240-chan FDM
         (channelWidth >= 15e6)):
#        (channelWidth >= 15e6 and nchan<96) or     # 2 pol TDM (128 chan)
#        (channelWidth >= 30e6 and nchan<96)):      # 4 pol TDM (64 chan)
        return True
    else:
        return False

def atmosphereVariation(img, header, chanInfo, airmass=1.5, pwv=-1, removeSlope=True):
    """
    Computes the absolute and percentage variation in atmospheric transmission 
    and sky temperature across an image cube.
    Returns 5 values: max(Trans)-min(Trans), and as percentage of mean,
                      Max(Tsky)-min(Tsky), and as percentage of mean, 
                      standard devation of Tsky
    """
    freqs, values = CalcAtmTransmissionForImage(img, header, chanInfo, airmass=airmass, pwv=pwv, value='transmission')
    if removeSlope:
        slope, intercept = linfit(freqs, values, values*0.001)
        casalogPost("Computed atmospheric variation and determined slope: %f per GHz (%.0f,%.2f)" % (slope,freqs[0],values[0]))
        values = values - (freqs*slope + intercept) + np.mean(values)
    maxMinusMin = np.max(values)-np.min(values)
    percentage = maxMinusMin/np.mean(values)
    freqs, values = CalcAtmTransmissionForImage(img, header, chanInfo, airmass=airmass, pwv=pwv, value='tsky')
    if removeSlope:
        slope, intercept = linfit(freqs, values, values*0.001)
        values = values - (freqs*slope + intercept) + np.mean(values)
    TmaxMinusMin = np.max(values)-np.min(values)
    Tpercentage = TmaxMinusMin*100/np.mean(values)
    stdValues = np.std(values)
    return(maxMinusMin, percentage, TmaxMinusMin, Tpercentage, stdValues)

def runFindContinuum(img='', spw='', transition='', baselineModeA='min', baselineModeB='min',
                     sigmaCube=3, nBaselineChannels=0.19, sigmaFindContinuum='auto',
                     verbose=False, png='', pngBasename=False, nanBufferChannels=2, 
                     source='', useAbsoluteValue=True, trimChannels='auto', 
                     percentile=20, continuumThreshold=None, narrow='auto', 
                     separator=';', overwrite=False, titleText='', 
                     showAverageSpectrum=False, maxTrim=maxTrimDefault, maxTrimFraction=1.0,
                     meanSpectrumFile='', centralArcsec=-1, channelWidth=0,
                     alternateDirectory='.', imageInfo=[], chanInfo=[], 
                     header='', plotAtmosphere='transmission', airmass=1.5, pwv=1.0,
                     channelFractionForSlopeRemoval=0.75, mask='', 
                     invert=False, meanSpectrumMethod='peakOverMad', 
                     peakFilterFWHM=15, fullLegend=False, iteration=0,
                     meanSpectrumMethodMessage='', minSlopeToRemove=1e-8,
                     minGroupsForSFCAdjustmentInPeakOverMad=10, 
                     regressionTest=False, quadraticFit=False, megapixels=0,
                     triangularPatternSeen=False, maxMemory=-1, negativeThresholdFactor=1.15,
                     byteLimit=-1, singleContinuum=False, applyMaskToMask=False, 
                     plotBaselinePoints=False, dropBaselineChannels=2.0,
                     madRatioUpperLimit=1.5, madRatioLowerLimit=1.15, projectCode=''):
    """
    This function calls functions to:
    1) compute the mean spectrum of a dirty cube
    2) find the continuum channels 
    3) plot the results
    Inputs: channelWidth: in Hz
    Returns: 6 items
    * A channel selection string suitable for the spw parameter of clean.
    * The name of the png produced.
    * The slope of the linear fit (or None if no fit attempted).
    * The channel width in Hz (only necessary for the fitsTable option).
    * nchan in the cube (only necessary for the fitsTable option).
    * Boolean: True if it used low values as the baseline (as opposed to high values)

    Inputs:
    img: the image cube to operate upon
    spw: the spw name or number to put in the x-axis label
    transition: the name of the spectral transition (for the plot title)
    baselineModeA: 'min' or 'edge', method to define the baseline in meanSpectrum()
    baselineModeB: 'min' or 'edge', method to define the baseline in findContinuumChannels()
    sigmaCube: multiply this value by the rms to get the threshold above which a pixel
               is included in the mean spectrum
    nBaselineChannels: if integer, then the number of channels to use in:
      a) computing the mean spectrum with the 'meanAboveThreshold' methods.
      b) computing the MAD of the lowest/highest channels in findContinuumChannels
          if float, then the fraction of channels to use (i.e. the percentile)
          default = 0.19, which is 24 channels (i.e. 12 on each side) of a TDM window
    sigmaFindContinuum: passed to findContinuumChannels, 'auto' starts with 3.5
    verbose: if True, then print additional information during processing
    png: the name of the png to produce ('' yields default name)
    pngBasename: if True, then remove the directory from img name before generating png name
    nanBufferChannels: when removing or replacing NaNs, do this many extra channels
                       beyond their extent
    source: the name of the source, to be shown in the title of the spectrum.
            if None, then use the filename, up to the first underscore.
    findContinuum: if True, then find the continuum channels before plotting
    overwrite: if True, or ASCII file does not exist, then recalculate the mean spectrum
                      writing it to <img>.meanSpectrum
               if False, then read the mean spectrum from the existing ASCII file
    trimChannels: after doing best job of finding continuum, remove this many 
         channels from each edge of each block of channels found (for margin of safety)
         If it is a float between 0..1, then trim this fraction of channels in each 
         group (rounding up). If it is 'auto', use 0.1 but not more than maxTrim channels
         and not more than maxTrimFraction
    percentile: control parameter used with baselineMode='min'
    continuumThreshold: if specified, only use pixels above this intensity level
    separator: the character to use to separate groups of channels in the string returned
    narrow: the minimum number of channels that a group of channels must have to survive
            if 0<narrow<1, then it is interpreted as the fraction of all
                           channels within identified blocks
            if 'auto', then use int(ceil(log10(nchan)))
    titleText: default is img name and transition and the control parameter values
    showAverageSpectrum: make a two-panel plot, showing the raw mean spectrum in black
    maxTrim: in trimChannels='auto', this is the max channels to trim per group for TDM spws; it is automatically scaled upward for FDM spws.
    maxTrimFraction: in trimChannels='auto', the max fraction of channels to trim per group
    meanSpectrumFile: an alternative ASCII text file to use for the mean spectrum.
                      Must also set img=''.
    meanSpectrumMethod: 'peakOverMad', 'peakOverRms', 'meanAboveThreshold', 
       'meanAboveThresholdOverRms', 'meanAboveThresholdOverMad', 
        where 'peak' refers to the peak in a spatially-smoothed version of cube
    peakFilterFWHM: value used by 'peakOverRms' and 'peakOverMad' to presmooth 
        each plane with a Gaussian kernel of this width (in pixels)
        Set to 1 to not do any smoothing.
    centralArcsec: radius of central box within which to compute the mean spectrum
        default='auto' means start with whole field, then reduce to 1/10 if only
        one window is found (unless mask is specified); or 'fwhm' for the central square
        with the same radius as the PB FWHM; or a floating point value in arcseconds
    mask: a mask image equal in shape to the parent image that is used to determine the
        region to compute the noise (outside the mask) and the mean spectrum (inside the mask)
        option 'auto' will look for the <filename>.mask that matches the <filename>.image
        and if it finds it, it will use it; otherwise it will use no mask
    plotAtmosphere: '', 'tsky', or 'transmission'
    airmass: for plot of atmospheric transmission
    pwv: in mm (for plot of atmospheric transmission)
    channelFractionForSlopeRemoval: if at least this many channels are initially selected, or 
       if there are only 1-2 ranges found and the widest range has > nchan*0.3 channels, then 
       fit and remove a linear slope and re-identify continuum channels.
    quadraticFit: if True, fit quadratic polynomial to the noise regions when appropriate; 
         otherwise fit only a linear slope
    megapixels: simply used to label the plot
    triangularPatternSeen: if True, then slightly boost sigmaFindContinuum if it is in 'auto' mode
    maxMemory: only used to name the png if it is set
    negativeThresholdFactor: scale the nominal negative threshold by this factor (to adjust 
        sensitivity to absorption features: smaller values=more sensitive)
    applyMaskToMask: if True, apply the mask inside the user mask image to set its masked pixels to 0
    plotBaselinePoints: if True, then plot the baseline-defining points as black dots
    dropBaselineChannels: percentage of extreme values to drop in baseline mode 'min'
    """
    casalogPost("%d) Current memory usage: %.3f GB, resident: %.3f GB" % (iteration+1, memoryUsage(), residentMemoryUsage()))
    startTime = timeUtilities.time()
    slope=None 
    replaceNans = True # This used to be a command-line option, but no longer.
    img = img.rstrip('/')
    fitsTable = False
    typeOfMeanSpectrum = 'Existing'
    narrowValueModified = None
    if (meanSpectrumFile != '' and os.path.exists(meanSpectrumFile)):
        casalogPost("Using existing meanSpectrumFile = %s" % (meanSpectrumFile))
        if (is_binary(meanSpectrumFile)):
            fitsTable = True
    if ((type(nBaselineChannels) == float or type(nBaselineChannels) == np.float64) and not fitsTable):
        # chanInfo will be == [] if an ASCII meanSpectrumFile is specified
        if len(chanInfo) >= 4:
            nchan, firstFreq, lastFreq, channelWidth = chanInfo
            channelWidth = abs(channelWidth)
            nBaselineChannels = int(round(nBaselineChannels*nchan))
            casalogPost("Found %d channels in the cube" % (nchan))
        
    if (nBaselineChannels < 2 and not fitsTable and len(chanInfo) >= 4):
        casalogPost("You must have at least 2 edge channels (nBaselineChannels = %d)" % (nBaselineChannels))
        return
    if (meanSpectrumFile == ''):
        meanSpectrumFile = buildMeanSpectrumFilename(img, meanSpectrumMethod, peakFilterFWHM)
    elif (not os.path.exists(meanSpectrumFile)):
        if (len(os.path.dirname(img)) > 0):
            meanSpectrumFile = os.path.dirname(img) + '/' + os.path.basename(meanSpectrumFile)
    if not overwrite:
        if (os.path.exists(meanSpectrumFile) and img != ''):
            if (maskArgumentMismatch(mask, meanSpectrumFile) and not fitsTable):
                casalogPost("Regenerating the meanSpectrum since there is a mismatch in the mask argument.")
                overwrite = True
            else:
                casalogPost("No mismatch in the mask argument vs. the meanSpectrum file.")
            if (centralArcsecArgumentMismatch(centralArcsec, meanSpectrumFile, iteration) and not fitsTable):
                casalogPost("Regenerating the meanSpectrum since there is a mismatch in the centralArcsec argument (%s)." % (str(centralArcsec)))
                overwrite = True
            else:
                casalogPost("No mismatch in the centralArcsec argument vs. the meanSpectrum file.")
        elif (img != ''):
            casalogPost("Did not find mean spectrum file = %s" % (meanSpectrumFile))
    if ((overwrite or not os.path.exists(meanSpectrumFile)) and not fitsTable):
        casalogPost("A) Current memory usage: %.3f GB, resident: %.3f GB" % (memoryUsage(), residentMemoryUsage()))
        if (overwrite):
            casalogPost("Regenerating the mean spectrum file with centralArcsec=%s, mask='%s'." % (str(centralArcsec),mask))
        else:
            casalogPost("Generating the mean spectrum file with centralArcsec=%s, mask='%s'." % (str(centralArcsec),mask))
        typeOfMeanSpectrum = 'Computed'
        result = meanSpectrum(img, nBaselineChannels, sigmaCube, verbose,
                              nanBufferChannels,useAbsoluteValue,
                              baselineModeA, percentile,
                              continuumThreshold, meanSpectrumFile, 
                              centralArcsec, imageInfo, chanInfo, mask,
                              meanSpectrumMethod, peakFilterFWHM, iteration, applyMaskToMask)
        casalogPost("C) Current memory usage: %.3f GB, resident: %.3f GB" % (memoryUsage(), residentMemoryUsage()))
        if result is None:
            return
        avgspectrum, avgSpectrumNansRemoved, avgSpectrumNansReplaced, meanSpectrumThreshold,\
          edgesUsed, nchan, nanmin, percentagePixelsNotMasked = result
        if verbose:
            print "len(avgspectrum) = %d, len(avgSpectrumNansReplaced)=%d" % (len(avgspectrum),len(avgSpectrumNansReplaced))
    else:
        if (fitsTable):
            result = readMeanSpectrumFITSFile(meanSpectrumFile)
            if (result is None):
                casalogPost("FITS table is not valid.")
                return
            avgspectrum, avgSpectrumNansReplaced, meanSpectrumThreshold, edgesUsed, nchan, nanmin, firstFreq, lastFreq = result
            percentagePixelsNotMasked = -1
        else:
            # An ASCII file was specified as the spectrum to process
            casalogPost("Running readPreviousMeanSpectrum('%s')" % (meanSpectrumFile))
            result = readPreviousMeanSpectrum(meanSpectrumFile, verbose, invert)
            if (result is None):
                casalogPost("ASCII file is not valid, re-run with overwrite=True")
                return
            avgspectrum, avgSpectrumNansReplaced, meanSpectrumThreshold, edgesUsed, nchan, nanmin, firstFreq, lastFreq, previousCentralArcsec, previousMask, percentagePixelsNotMasked = result
            # Note: previousCentralArcsec  and   previousMask  are not used yet
        chanInfo = [result[4], result[6], result[7], abs(result[7]-result[6])/(result[4]-1)]
        if verbose:
            print "len(avgspectrum) = ", len(avgspectrum)
        if (len(avgspectrum) < 2):
            casalogPost("ASCII file is too short, re-run with overwrite=True")
            return
        if (firstFreq == 0 and lastFreq == 0):
            # This was an old-format ASCII file, without a frequency column
            n, firstFreq, lastFreq, channelWidth = chanInfo
            channelWidth = abs(channelWidth)
        if (fitsTable or img==''):
            nBaselineChannels = int(round(nBaselineChannels*nchan))
            n, firstFreq, lastFreq, channelWidth = chanInfo
            channelWidth = abs(channelWidth)
            print "Setting channelWidth to %g" % (channelWidth)
            if (nBaselineChannels < 2):
                casalogPost("You must have at least 2 edge channels")
                return
            
    donetime = timeUtilities.time()
    casalogPost("%.1f sec elapsed in meanSpectrum()" % (donetime-startTime))
    casalogPost("Iteration %d" % (iteration))
    if singleContinuum:
        sFC_TDM = 9.0 
    elif (meanSpectrumMethod.find('meanAboveThreshold') >= 0):
        sFC_TDM = 4.5
    else:
        sFC_TDM = 6.5
    if (sigmaFindContinuum == 'auto' or sigmaFindContinuum == -1):
        sigmaFindContinuumAutomatic = True
        if (tdmSpectrum(channelWidth, nchan)):
            sigmaFindContinuum = sFC_TDM
            casalogPost("Setting sigmaFindContinuum = %.1f since it is TDM" % (sFC_TDM))
        elif (meanSpectrumMethod.find('meanAboveThreshold') >= 0):
            sigmaFindContinuum = 3.5
            casalogPost("Setting sigmaFindContinuum = %.1f since we are using meanAboveThreshold" % (sigmaFindContinuum))
        else:
            sigmaFindContinuum = 6.0
            casalogPost("Setting sigmaFindContinuum = %.1f since we are using peakOverMAD" % (sigmaFindContinuum))
        if triangularPatternSeen:
            sigmaFindContinuum += 0.5
            casalogPost("Adding 0.5 to sigmaFindContinuum")
    else:
        sigmaFindContinuumAutomatic = False
    continuumChannels,selection,threshold,median,groups,correctionFactor,medianTrue,mad,medianCorrectionFactor,negativeThreshold,lineStrengthFactor,singleChannelPeaksAboveSFC,allGroupsAboveSFC,spectralDiff, trimChannels, useLowBaseline, narrowValueModified, allBaselineChannelsXY, madRatio = \
        findContinuumChannels(avgSpectrumNansReplaced, nBaselineChannels, sigmaFindContinuum, nanmin, 
                              baselineModeB, trimChannels, narrow, verbose, maxTrim, maxTrimFraction, separator,
                              negativeThresholdFactor=negativeThresholdFactor, 
                              dropBaselineChannels=dropBaselineChannels,
                              madRatioUpperLimit=madRatioUpperLimit, 
                              madRatioLowerLimit=madRatioLowerLimit)
    sumAboveMedian, sumBelowMedian, sumRatio, channelsAboveMedian, channelsBelowMedian, channelRatio = \
        aboveBelow(avgSpectrumNansReplaced,medianTrue)
    # First, one group must have at least 2 channels (to insure it is real), otherwise raise the sigmaFC.
    # Otherwise, if there are a lot of groups or a lot of channels above the median compared to below it,
    # then lower the sigma in order to push the threshold for real lines (or line emission wings) lower.
    # However, if there is only 1 group, then there may be no real lines present, so lowering 
    # the threshold in this case can create needless extra groups, so don't allow it.
    spwBandwidth = nchan*channelWidth
    factor = 1.0
    newMaxTrim = 0
    if (groups <= 2 and not tdmSpectrum(channelWidth, nchan) and 
        maxTrim==maxTrimDefault and trimChannels=='auto'):
        # CAS-8822
        newMaxTrim = maxTrimDefault*nchan/128
        casalogPost("Setting maxTrim from %s to %s for this FDM spw because trimChannels='%s' and groups=%d." % (str(maxTrim),str(newMaxTrim),trimChannels,groups))
        maxTrim = newMaxTrim
    if (singleChannelPeaksAboveSFC == allGroupsAboveSFC and allGroupsAboveSFC>1):
        if (sigmaFindContinuum < sFC_TDM):  # this seems to screw up on G19.01_B7 spw3
            # raise the threshold a bit since all the peaks look like all noise
            factor = 1.5
            sigmaFindContinuum *= factor
            casalogPost("Scaling the threshold upward by a factor of %.2f to avoid apparent noise spikes (%d==%d)." % (factor, singleChannelPeaksAboveSFC,allGroupsAboveSFC))
            continuumChannels,selection,threshold,median,groups,correctionFactor,medianTrue,mad,medianCorrectionFactor,negativeThreshold,lineStrengthFactor,singleChannelPeaksAboveSFC,allGroupsAboveSFC,spectralDiff,trimChannels,useLowBaseline, narrowValueModified, allBaselineChannelsXY, madRatio = \
                findContinuumChannels(avgSpectrumNansReplaced, nBaselineChannels, sigmaFindContinuum, nanmin, 
                                      baselineModeB, trimChannels, narrow, verbose, maxTrim, maxTrimFraction, 
                                      separator, negativeThresholdFactor=negativeThresholdFactor,
                                      dropBaselineChannels=dropBaselineChannels,
                                      madRatioUpperLimit=madRatioUpperLimit, 
                                      madRatioLowerLimit=madRatioLowerLimit)
            sumAboveMedian, sumBelowMedian, sumRatio, channelsAboveMedian, channelsBelowMedian, channelRatio = \
                aboveBelow(avgSpectrumNansReplaced,medianTrue)
    elif ((groups > 3 or (groups > 1 and channelRatio < 1.0) or (channelRatio < 0.5) or (groups == 2 and channelRatio < 1.3)) and sigmaFindContinuumAutomatic and meanSpectrumMethod.find('peakOver') < 0 and not singleContinuum):
        print "A: groups,channelRatio=", groups, channelRatio, channelRatio < 1.0, channelRatio>0.1, tdmSpectrum(channelWidth,nchan), groups>2
        if (channelRatio < 0.9 and channelRatio > 0.1 and (firstFreq>60e9 and not tdmSpectrum(channelWidth,nchan)) and groups>2):  # was nchan>256
            # Don't allow this much reduction in ALMA TDM mode as it chops up line-free quasar spectra too much.
            # The channelRatio>0.1 requirement prevents failures due to ALMA TFB platforming.
            factor = 0.333
        elif (groups <= 2):
            if (0.1 < channelRatio < 1.3 and groups == 2 and 
                not tdmSpectrum(channelWidth,nchan) and channelWidth>=1875e6/600.):
                # the /480 above should really be /600 to avoid small differences in channel width
                if (channelWidth < 1875e6/360.):
                    if madRatioLowerLimit < madRatio < madRatioUpperLimit:
                        factor = 0.60
                    else:
                        factor = 0.50
                    # i.e. for galaxy spectra with FDM 480 channel (online-averaging) resolution
                    # but 0.5 is too low for uid___A001_X879_X47a.s24_0.ELS26_sci.spw25.mfs.I.findcont.residual
                    # and for uid___A001_X2d8_X2c5.s24_0.2276_444_53712_sci.spw16.mfs.I.findcont.residual
                    #    the latter requires >=0.057
                    #   need to reconcile in future versions
                else:
                    factor = 0.7  # i.e. for galaxy spectra with FDM 240 channel (online-averaging) resolution
            else:
                # prevent sigmaFindContinuum going to inf if groups==1
                # prevent sigmaFindContinuum going > 1 if groups==2
                factor = 0.9
        else:
            if tdmSpectrum(channelWidth,nchan):
                factor = 1.0
            elif channelWidth>0:  # the second factor tempers the reduction as the spw bandwidth decreases
                factor = (np.log(3)/np.log(groups)) ** (spwBandwidth/1.875e9)
            else:
                factor = (np.log(3)/np.log(groups))
        casalogPost("setting factor to %f because groups=%d, channelRatio=%g, firstFreq=%g, nchan=%d, channelWidth=%e" % (factor,groups,channelRatio,firstFreq,nchan,channelWidth))
        print "---------------------"
        casalogPost("Scaling the threshold by a factor of %.2f (groups=%d, channelRatio=%f)" % (factor, groups,channelRatio))
        print "---------------------"
        sigmaFindContinuum *= factor
        continuumChannels,selection,threshold,median,groups,correctionFactor,medianTrue,mad,medianCorrectionFactor,negativeThreshold,lineStrengthFactor,singleChannelPeaksAboveSFC,allGroupsAboveSFC,spectralDiff,trimChannels,useLowBaseline, narrowValueModified, allBaselineChannelsXY, madRatio = \
            findContinuumChannels(avgSpectrumNansReplaced, nBaselineChannels, sigmaFindContinuum, nanmin, 
                                  baselineModeB, trimChannels, narrow, verbose, maxTrim, maxTrimFraction, 
                                  separator, negativeThresholdFactor=negativeThresholdFactor, 
                                  dropBaselineChannels=dropBaselineChannels,
                                  madRatioUpperLimit=madRatioUpperLimit, 
                                  madRatioLowerLimit=madRatioLowerLimit)
        sumAboveMedian, sumBelowMedian, sumRatio, channelsAboveMedian, channelsBelowMedian, channelRatio = \
            aboveBelow(avgSpectrumNansReplaced,medianTrue)
    else:
        if (meanSpectrumMethod.find('peakOver') < 0):
            casalogPost("Not adjusting sigmaFindContinuum, because groups=%d, channelRatio=%g, firstFreq=%g, nchan=%d" % (groups,channelRatio,firstFreq,nchan))
        else:
            # We are using peakOverMad
            if (groups > minGroupsForSFCAdjustmentInPeakOverMad and not tdmSpectrum(channelWidth,nchan)):
                # Heuristics for sFC to get better results on hot cores when meanAboveThreshold cannot be used.
                maxGroups = 40
                if groups < maxGroups:
                    factor = np.max([2.5/sigmaFindContinuum, (1-groups/float(maxGroups)) ** (spwBandwidth/1.875e9)])
                else:
                    factor = 2.5/sigmaFindContinuum
                sigmaFindContinuum *= factor
                casalogPost("Adjusting sigmaFindContinuum to %f because groups>%d and not TDM and meanSpectrumMethod = %s" % (sigmaFindContinuum, minGroupsForSFCAdjustmentInPeakOverMad, meanSpectrumMethod))
            else:
                casalogPost("Not adjusting sigmaFindContinuum because meanSpectrumMethod = %s" % (meanSpectrumMethod))
                
        if (newMaxTrim > 0 or 
            (groups>minGroupsForSFCAdjustmentInPeakOverMad and not tdmSpectrum(channelWidth,nchan))): # added Aug 22, 2016
            if (newMaxTrim > 0):
                casalogPost("But re-running findContinuumChannels with new maxTrim")
            continuumChannels,selection,threshold,median,groups,correctionFactor,medianTrue,mad,medianCorrectionFactor,negativeThreshold,lineStrengthFactor,singleChannelPeaksAboveSFC,allGroupsAboveSFC,spectralDiff,trimChannels, useLowBaseline, narrowValueModified, allBaselineChannelsXY, madRatio = \
                findContinuumChannels(avgSpectrumNansReplaced, nBaselineChannels, sigmaFindContinuum, nanmin, 
                                      baselineModeB, trimChannels, narrow, verbose, maxTrim, maxTrimFraction, 
                                      separator, negativeThresholdFactor=negativeThresholdFactor, 
                                      dropBaselineChannels=dropBaselineChannels,
                                      madRatioUpperLimit=madRatioUpperLimit, 
                                      madRatioLowerLimit=madRatioLowerLimit)
            sumAboveMedian, sumBelowMedian, sumRatio, channelsAboveMedian, channelsBelowMedian, channelRatio = \
                aboveBelow(avgSpectrumNansReplaced,medianTrue)

    selectedChannels = countChannels(selection)
    largestGroup = channelsInLargestGroup(selection)
    selections = len(selection.split(separator))
    slopeRemoved = False
    channelDistancesFromCenter = np.array(continuumChannels)-nchan/2
    channelDistancesFromLowerThird = np.array(continuumChannels)-nchan*0.3
    channelDistancesFromUpperThird = np.array(continuumChannels)-nchan*0.7
    if len(np.where(channelDistancesFromCenter > 0)[0]) > 0 and len(np.where(channelDistancesFromCenter < 0)[0]) > 0:
        channelsInBothHalves = True
    else:
        # casalogPost("channelDistancesFromCenter = %s" % (channelDistancesFromCenter))
        channelsInBothHalves = False  # does not get triggered by case 115, which could use it
    if ((len(np.where(channelDistancesFromLowerThird < 0)[0]) > 0) and 
        (len(np.where(channelDistancesFromUpperThird > 0)[0]) > 0)):
        channelsInBothEdgeThirds = True
    else:
        channelsInBothEdgeThirds = False
    # too many selected windows means there might be a lot of lines, so don't do a baseline fit
    maxSelections = 4 # 2    # July 27, 2017
    # too few channels means you cannot get a reliable fit
    minBWFraction = 0.3
    if (selectedChannels > channelFractionForSlopeRemoval*nchan or 
        (selectedChannels > 0.4*nchan and selections==2) or 
        # fix for project 00956 spw 25 is to put lower bound of 1 < selections:
        (largestGroup>nchan*minBWFraction and 1 < selections <= maxSelections and 
         channelFractionForSlopeRemoval<1)):
        previousResult = continuumChannels,selection,threshold,median,groups,correctionFactor,medianTrue,mad,medianCorrectionFactor,negativeThreshold,lineStrengthFactor,singleChannelPeaksAboveSFC,allGroupsAboveSFC        
        # remove linear slope from mean spectrum and run it again
        index = channelSelectionRangesToIndexArray(selection)
#        if quadraticFit and (channelsInBothEdgeThirds or (channelsInBothHalves and selections==1)):  # July 27, 2017
        quadraticFitTest = quadraticFit and ((channelsInBothEdgeThirds and selections <= 2) or (channelsInBothHalves and selections==1))
        if quadraticFitTest:
            casalogPost("Fitting quadratic to %d channels (largestGroup=%d,nchan*%.1f=%.1f,selectedChannels=%d)" % (len(index), largestGroup, minBWFraction, nchan*minBWFraction, selectedChannels))
            fitResult = polyfit(index, avgSpectrumNansReplaced[index], MAD(avgSpectrumNansReplaced[index]))
            order2, slope, intercept, xoffset = fitResult
        else:
            casalogPost("Fitting slope to %d channels (largestGroup=%d,nchan*%.1f=%.1f,selectedChannels=%d)" % (len(index), largestGroup, minBWFraction, nchan*minBWFraction, selectedChannels))
            fitResult = linfit(index, avgSpectrumNansReplaced[index], MAD(avgSpectrumNansReplaced[index]))
            slope, intercept = fitResult
        if (factor >= 1.5 and maxTrim==maxTrimDefault and trimChannels=='auto'):
            #                 add these 'and' cases on July 20, 2016
            # Do not restore if we have modified maxTrim.  This prevents breaking up an FDM
            # spectrum with no line detection into multiple smaller continuum windows.
            sigmaFindContinuum /= factor
            casalogPost("Restoring sigmaFindContinuum to %f" % (sigmaFindContinuum))
            rerun = True
        else:
            rerun = False
        if quadraticFitTest:
#        if quadraticFit and (channelsInBothEdgeThirds or (channelsInBothHalves and selections==1)):
            casalogPost("Removing quadratic = %g*(x-%f)**2 + %g*(x-%f) + %g" % (order2,xoffset,slope,xoffset,intercept))
            myx = np.arange(len(avgSpectrumNansReplaced)) - xoffset
            priorMad = MAD(avgSpectrumNansReplaced)
            trialSpectrum = avgSpectrumNansReplaced + nanmean(avgSpectrumNansReplaced)-(myx**2*order2 + myx*slope + intercept)
            postMad =  MAD(trialSpectrum)
            if postMad > priorMad:
                casalogPost("MAD of spectrum got worse after quadratic removal: %f to %f. Switching to linear fit." % (priorMad, postMad), debug=True)
                casalogPost("Fitting slope to %d channels (largestGroup=%d,nchan*%.1f=%.1f,selectedChannels=%d)" % (len(index), largestGroup, minBWFraction, nchan*minBWFraction, selectedChannels))
                fitResult = linfit(index, avgSpectrumNansReplaced[index], MAD(avgSpectrumNansReplaced[index]))
                slope, intercept = fitResult
                if (abs(slope) > minSlopeToRemove):
                    casalogPost("Removing slope = %g" % (slope))
                    # Do not remove the offset in order to avoid putting spectrum near zero
                    avgSpectrumNansReplaced -= np.array(range(len(avgSpectrumNansReplaced)))*slope
                    slopeRemoved = True
                    rerun = True
            else:
                avgSpectrumNansReplaced = trialSpectrum
                casalogPost("MAD of spectrum improved after quadratic removal:  %f to %f" % (priorMad, postMad), debug=True)
            slopeRemoved = True
            rerun = True
            if lineStrengthFactor < 1.2:
                if sigmaFindContinuumAutomatic:
                    sigmaFindContinuum += 0.5
                    casalogPost("lineStrengthFactor = %f < 1.2 (increasing sigmaFindContinuum by 0.5 to %.1f)" % (lineStrengthFactor,sigmaFindContinuum)) 
                else:
                    casalogPost("lineStrengthFactor = %f < 1.2 (but not increasing sigmaFindContinuum by 0.5 because it is not 'auto')" % (lineStrengthFactor))
            else:
                casalogPost("lineStrengthFactor = %f >= 1.2" % (lineStrengthFactor)) 
        else:
            if (abs(slope) > minSlopeToRemove):
                casalogPost("Removing slope = %g" % (slope))
                avgSpectrumNansReplaced -= np.array(range(len(avgSpectrumNansReplaced)))*slope
                slopeRemoved = True
                rerun = True
# The following helped deliver more continuum bandwidth for HD_142527 spw3, but
# it harmed 2013.1.00518 13co (and probably others) by including wing emission.
#            if trimChannels == 'auto':   # prevent overzealous trimming  July 20, 2016
#                maxTrim = maxTrimDefault # prevent overzealous trimming  July 20, 2016
        discardSlopeResult = False
        if rerun:
            continuumChannels,selection,threshold,median,groups,correctionFactor,medianTrue,mad,medianCorrectionFactor,negativeThreshold,lineStrengthFactor,singleChannelPeaksAboveSFC,allGroupsAboveSFC,spectralDiff,trimChannels,useLowBaseline, narrowValueModified, allBaselineChannelsXY, madRatio = \
            findContinuumChannels(avgSpectrumNansReplaced, nBaselineChannels, sigmaFindContinuum, nanmin, 
                                  baselineModeB, trimChannels, narrow, verbose, maxTrim, maxTrimFraction, 
                                  separator, fitResult, negativeThresholdFactor=negativeThresholdFactor, 
                                  dropBaselineChannels=dropBaselineChannels,
                                  madRatioUpperLimit=madRatioUpperLimit, 
                                  madRatioLowerLimit=madRatioLowerLimit)

            # If we had only one group and only added one or two more group after removing slope, and the
            # smallest is small compared to the original group, then discard the new solution.
            if (groups <= 3 and previousResult[4] == 1):
                counts = countChannelsInRanges(selection)
                if (float(min(counts))/max(counts) < 0.2):
                    casalogPost("*** Restoring result prior to linfit because %d/%d < 0.2***" % (min(counts),max(counts)))
                    discardSlopeResult = True
                    continuumChannels,selection,threshold,median,groups,correctionFactor,medianTrue,mad,medianCorrectionFactor,negativeThreshold,lineStrengthFactor,singleChannelPeaksAboveSFC,allGroupsAboveSFC = previousResult
    else:
        casalogPost("No slope fit attempted because selected channels (%d) < %.2f * nchan(%d) or other criteria not met" % (selectedChannels,channelFractionForSlopeRemoval,nchan))
        casalogPost("  largestGroup=%d <= nchan*%.1f=%.1f or selections=%d > %d or  channelFractionForSlopeRemoval=%f>=1" % (largestGroup,minBWFraction,nchan*minBWFraction,selections,maxSelections,channelFractionForSlopeRemoval))
  
    idx = np.where(avgSpectrumNansReplaced < threshold)
    madOfPointsBelowThreshold = MAD(avgSpectrumNansReplaced[idx])
    pl.clf()
    if (showAverageSpectrum):
        rows = 2
    else:
        rows = 1
    cols = 1
    fontsize = 10
    ax1 = pl.subplot(rows,cols,1)
    skipchan = 1 # was 2
    if replaceNans:
        avgspectrumAboveThreshold = avgSpectrumNansReplaced
    else:
        avgspectrumAboveThreshold = avgSpectrumNansRemoved
    if (edgesUsed == 2):
        if (showAverageSpectrum):
            pl.plot(range(len(avgspectrum)), avgspectrum, 'k-')
            pl.ylabel('Average spectrum', size=fontsize)
            ylimTop = pl.ylim()
            pl.subplot(rows,cols,2)
        pl.plot(range(len(avgspectrumAboveThreshold)), 
                avgspectrumAboveThreshold, 'r-')
        drawYlabel(img, typeOfMeanSpectrum,meanSpectrumMethod, meanSpectrumThreshold,
                   peakFilterFWHM, fontsize)
    elif (edgesUsed == 0):
        # The upper edge is not used and can have an upward spike
        # so don't show it.
        casalogPost("Not showing final %d channels of %d" % (skipchan,\
               len(avgspectrumAboveThreshold)))
        if (showAverageSpectrum):
            pl.plot(range(len(avgspectrum)), avgspectrum, 'k-')
            pl.ylabel('average spectrum')
            ylimTop = pl.ylim()
            ax1.ylim(ylim)
            pl.subplot(rows,cols,2)
        pl.plot(range(len(avgspectrumAboveThreshold)-skipchan), 
                avgspectrumAboveThreshold[:-skipchan], 'r-')
        drawYlabel(img, typeOfMeanSpectrum,meanSpectrumMethod, meanSpectrumThreshold, 
                   peakFilterFWHM, fontsize)
    elif (edgesUsed == 1):
        # The lower edge channels are not used and the threshold mean can 
        # have an upward spike, so don't show the first channel inward from 
        # there.
        casalogPost("Not showing first %d channels of %d" % (skipchan,\
                                   len(avgspectrumAboveThreshold)))
        if (showAverageSpectrum):
            pl.plot(range(len(avgspectrum)), avgspectrum, 'k-')
            pl.ylabel('average spectrum')
            ylimTop = pl.ylim()
            pl.subplot(rows,cols,2)
        pl.plot(range(skipchan,len(avgspectrumAboveThreshold)), 
                avgspectrumAboveThreshold[skipchan:], 'r-')
        drawYlabel(img, typeOfMeanSpectrum,meanSpectrumMethod, meanSpectrumThreshold, 
                   peakFilterFWHM, fontsize)
        
    pl.hold(True)
    if (baselineModeA == 'edge'):
        nEdgeChannels = nBaselineChannels/2
        if (edgesUsed == 0 or edgesUsed == 2):
            pl.plot(range(nEdgeChannels), avgspectrum[:nEdgeChannels], 'm-',lw=3)
        if (edgesUsed == 1 or edgesUsed == 2):
            pl.plot(range(nchan-nEdgeChannels,nchan), avgspectrum[-nEdgeChannels:],
                    'm-', lw=3)
    if plotBaselinePoints:
        pl.plot(allBaselineChannelsXY[0], allBaselineChannelsXY[1], 'ko')
    channelSelections = []
    casalogPost('Drawing positive threshold at %g' % (threshold))
    pl.plot(pl.xlim(), [threshold,threshold], 'k:')
    if (negativeThreshold is not None):
        pl.plot(pl.xlim(), [negativeThreshold,negativeThreshold], 'k:')
        casalogPost('Drawing negative threshold at %g' % (negativeThreshold))
    casalogPost('Drawing observed median at %g' % (median))
    pl.plot(pl.xlim(), [median,median], 'k--')  # observed median (always lower than true for mode='min')
    casalogPost('Drawing inferredMedian at %g' % (medianTrue))
    pl.plot(pl.xlim(), [medianTrue,medianTrue], 'k-')
    if (baselineModeB == 'edge'):
        pl.plot([nEdgeChannels, nEdgeChannels], pl.ylim(), 'k:')
        pl.plot([nchan-nEdgeChannels, nchan-nEdgeChannels], pl.ylim(), 'k:')
    if (len(continuumChannels) > 0):
        channelSelections = selection.split(separator) # was ','
        for i,ccstring in enumerate(channelSelections): 
            cc = [int(j) for j in ccstring.split('~')]
            pl.plot(cc, np.ones(len(cc))*np.mean(avgspectrumAboveThreshold), 'c-', lw=2)
            yoffset = np.mean(avgspectrumAboveThreshold)+0.04*(pl.ylim()[1]-pl.ylim()[0])
            pl.text(np.mean(cc), yoffset, ccstring, va='bottom', ha='center',size=8,rotation=90)

    if (fitsTable or img==''):
        img = meanSpectrumFile
    if (source is None):
        source = os.path.basename(img)
        if (not fitsTable):
            source = source.split('_')[0]
    if (titleText == ''):
        narrowString = pickNarrowString(narrow, len(avgSpectrumNansReplaced), narrowValueModified) 
        trimString = pickTrimString(trimChannels, len(avgSpectrumNansReplaced), maxTrim)
        if len(projectCode) > 0: 
            projectCode += ', '
        titleText = projectCode + os.path.basename(img) + ' ' + transition
    ylim = pl.ylim()
    ylim = [ylim[0], ylim[1]+(ylim[1]-ylim[0])*0.2]
    xlim = [0,nchan-1]
    pl.xlim(xlim)
    titlesize = np.min([fontsize,int(np.floor(fontsize*100.0/len(titleText)))])
    if (spw != ''):
        label = '(Spw %s) Channels (%d)' % (str(spw), nchan)
        if singleContinuum:
            label = 'SingleCont ' + label
    else:
        label = 'Channels (%d)' % (nchan)
        if singleContinuum:
            label = '(SingleContinuum) ' + label
    if (showAverageSpectrum):
        pl.subplot(rows,cols,1)
        pl.plot(xlim, [threshold,threshold],'k:')
        pl.ylim(ylimTop)
        lowerplot = pl.subplot(rows,cols,2)
        pl.setp(ax1.get_yticklabels(), fontsize=fontsize)
        pl.text(0.5, 1.03, titleText, size=titlesize, 
                ha='center', transform=lowerplot.transAxes)
        pl.xlabel(label, size=fontsize)
    else:
        ax1.set_xlabel(label, size=fontsize)
        pl.text(0.5, 1.08, titleText, size=titlesize, ha='center', transform=ax1.transAxes)
    pl.ylim(ylim)
    ax2 = ax1.twiny()
    pl.setp(ax1.get_xticklabels(), fontsize=fontsize)
    pl.setp(ax1.get_yticklabels(), fontsize=fontsize)
    pl.setp(ax2.get_xticklabels(), fontsize=fontsize)
    ax2.set_xlim(firstFreq*1e-9,lastFreq*1e-9)
    freqRange = np.abs(lastFreq-firstFreq)
    power = int(np.log10(freqRange))-9
    ax2.xaxis.set_major_locator(matplotlib.ticker.MultipleLocator(10**power))
    if (len(ax2.get_xticks()) < 2):
        ax2.xaxis.set_major_locator(matplotlib.ticker.MultipleLocator(0.5*10**power))
    ax2.xaxis.set_minor_locator(matplotlib.ticker.MultipleLocator(0.1*10**power))
    ax2.xaxis.set_major_formatter(matplotlib.ticker.ScalarFormatter(useOffset=False))
    aggregateBandwidth = computeBandwidth(selection, channelWidth, 1)
    if (channelWidth > 0):
        channelWidthString = ', channel width: %g kHz, BW: %g MHz, contBW: %g MHz' % (channelWidth*1e-3, spwBandwidth*1e-6, aggregateBandwidth*1000)
    else:
        channelWidthString = ''
    freqType = ''
    if (type(header) == dict):
        if ('reffreqtype' in header.keys()):
            freqType = header['reffreqtype']
    if (spw != ''):
        label = '(Spw %s) %s Frequency (GHz)' % (str(spw),freqType) + channelWidthString
    else:
        label = '%s Frequency (GHz)' % freqType + channelWidthString
    ax2.set_xlabel(label, size=fontsize)
    inc = 0.03
    if showAverageSpectrum: inc *= 2
    i = 1
    if madRatio is not None:
        pl.text(0.5,0.01,'madRatio = %g'%(madRatio), ha='center', size=fontsize, transform=ax1.transAxes)
    effectiveSigma = sigmaFindContinuum*correctionFactor
    if meanSpectrumMethod.find('mean')>=0:
        pl.text(0.5,0.99-i*inc,'bl=(%s,%s), narrow=%s, sCube=%.1f, sigmaFC=%.2f*%.2f=%.2f, trim=%s' % (baselineModeA,baselineModeB,narrowString,sigmaCube,sigmaFindContinuum,correctionFactor,effectiveSigma,trimString),transform=ax1.transAxes, ha='center',size=fontsize)
    else:
        pl.text(0.5,0.99-i*inc,' baselineModeB=%s, narrow=%s, sigmaFC=%.2f*%.2f=%.2f, trim=%s' % (baselineModeB,narrowString,sigmaFindContinuum,correctionFactor,effectiveSigma,trimString),transform=ax1.transAxes, ha='center',size=fontsize)
    i += 1
    peak = np.max(avgSpectrumNansReplaced)
    peakFeatureSigma = (peak-medianTrue)/mad
    if (fullLegend):
        pl.text(0.5,0.99-i*inc,'rms=MAD*1.482: of baseline chans = %f, scaled by %.1f for all chans = %f'%(mad,correctionFactor,mad*correctionFactor), 
                transform=ax1.transAxes, ha='center',size=fontsize)
        i += 1
        pl.text(0.017,0.99-i*inc,'lineStrength factor: %.2f' % (lineStrengthFactor), transform=ax1.transAxes, ha='left', size=fontsize)
        pl.text(0.983,0.99-i*inc,'MAD*1.482: of points below upper dotted line = %f' % (madOfPointsBelowThreshold),
                transform=ax1.transAxes, ha='right', size=fontsize)
        i += 1
        pl.text(0.5,0.99-i*inc,'median: of %d baseline chans = %f, offset by %.1f*MAD for all chans = %f'%(nBaselineChannels, median,medianCorrectionFactor,medianTrue-median), 
                transform=ax1.transAxes, ha='center', size=fontsize)
        i += 1
        pl.text(0.5,0.99-i*inc,'chans>median: %d (sum=%.4f), chans<median: %d (sum=%.4f), ratio: %.2f (%.2f)'%(channelsAboveMedian,sumAboveMedian,channelsBelowMedian,sumBelowMedian,channelRatio,sumRatio),
                transform=ax1.transAxes, ha='center', size=fontsize-1)
    if (negativeThreshold is not None):
        pl.text(0.5,0.99-i*inc,'mad: %.3g; levs: %.3g, %.3g (dot); median: %.3g (solid), medmin: %.3g (dash)'%(mad, threshold,negativeThreshold,medianTrue,median), transform=ax1.transAxes, ha='center', size=fontsize-1)
    else:
        pl.text(0.5,0.99-i*inc,'mad: %.3g; threshold: %.3g (dot); median: %.3g (solid), medmin: %.3g (dash)'%(mad,threshold,medianTrue,median), 
                transform=ax1.transAxes, ha='center', size=fontsize)
    i += 1
    areaString = 'max: %.1f*mad=%g; maxTrimFrac=%g; %d ranges; ' % (peakFeatureSigma, peak, maxTrimFraction, len(channelSelections))
    if (centralArcsec == 'auto'):
        areaString += 'mean over area: (unknown)'
    elif (centralArcsec < 0):
        areaString += 'mean over area: whole field (%.2fMpix)' % (megapixels)
    else:
        areaString += 'mean over: central box of radius %.1f" (%.2fMpix)' % (centralArcsec,megapixels)
    pl.text(0.5,0.99-i*inc,areaString, transform=ax1.transAxes, ha='center', size=fontsize-1)
    if (meanSpectrumMethodMessage != ''):
#        msmm_ylabel = 0.01
        msmm_ylabel = -0.10
        pl.text(0.5,msmm_ylabel,meanSpectrumMethodMessage, 
                transform=ax1.transAxes, ha='center', size=fontsize)
        
    finalLine = ''
    if (len(mask) > 0):
        if (percentagePixelsNotMasked > 0):
            finalLine += 'mask=%s (%.2f%% pixels)' % (os.path.basename(mask), percentagePixelsNotMasked)
        else:
            finalLine += 'mask=%s' % (os.path.basename(mask))
        i += 1
        pl.text(0.5, 0.99-i*inc, finalLine, transform=ax1.transAxes, ha='center', size=fontsize)
        finalLine = ''
    if (slope is not None):
        if discardSlopeResult:
            discarded = ' (result discarded)'
        elif slopeRemoved:
            discarded = ' (removed)'
        else:
            discarded = ' (not removed)'
        if quadraticFitTest:
#        if quadraticFit and (channelsInBothEdgeThirds or (channelsInBothHalves and selections==1)):
            finalLine += 'quadratic fit: %g*(x-%g)**2+%g*(x-%g)+%g %s' % (roundFigures(order2,3),roundFigures(xoffset,4),roundFigures(slope,3),roundFigures(xoffset,4),roundFigures(intercept,3),discarded)
        else:
            finalLine += 'linear slope: %g %s' % (roundFigures(slope,3),discarded)
    i += 1
    pl.text(0.5, 0.99-i*inc, finalLine, transform=ax1.transAxes, ha='center', size=fontsize)
    i += 1
#    if (img != meanSpectrumFile):
#        a,b,c,d,e = atmosphereVariation(img, header, chanInfo, airmass=airmass, pwv=pwv)
#        finalLine = 'transmission: %f, %f  skyTemp: %f, %f' % (a,b,c,d)
#        pl.text(0.5, 0.99-i*inc, finalLine, transform=ax1.transAxes, ha='center', size=fontsize)
    gigabytes = getMemorySize()/(1024.**3)
    if not regressionTest:
        # Write CVS version
        pl.text(1.06, -0.005-2*inc, ' '.join(version().split()[1:4]), size=8, 
                transform=ax1.transAxes, ha='right')
        # Write CASA version
        if casaMajorVersion < 5:
            casaText = "CASA "+casadef.casa_version+" r"+casadef.subversion_revision+' (%.0fGB'%gigabytes
        else:
            casaText = "CASA "+cu.version_info()+' (%.0fGB' % (gigabytes)
    else:
        casaText = '(%.0fGB' % gigabytes
    byteLimit = byteLimit/(1024.**3)
    if maxMemory < 0 or byteLimit == gigabytes:
        casaText += ')'
    else:
        casaText += ', but limited to %.0fGB)' % (byteLimit)
    pl.text(-0.03, -0.005-2*inc, casaText, size=8, transform=ax1.transAxes, ha='left')
    if (plotAtmosphere != '' and img != meanSpectrumFile):
        if (plotAtmosphere == 'tsky'):
            value = 'tsky'
        else:
            value = 'transmission'
        freqs, atm = CalcAtmTransmissionForImage(img, header, chanInfo, airmass, pwv, value=value)
        casalogPost("freqs: min=%g, max=%g n=%d" % (np.min(freqs),np.max(freqs), len(freqs)))
        atmRange = 0.5  # how much of the y-axis should it take up
#        print "initial ylim = ", ylim
        yrange = ylim[1]-ylim[0]
#        print "yrange = ", yrange
#        print "before plot: ylim=%.6f,%.6f, mean atm=%f " % (ylim[0], ylim[1], np.mean(atm))
        if (value == 'transmission'):
            atmRescaled = ylim[0] + 0.3*yrange + atm*atmRange*yrange
            print "atmRescaled: min=%f, max=%f" % (np.min(atmRescaled), np.max(atmRescaled))
            pl.text(1.015, 0.3, '0%', color='m', transform=ax1.transAxes, ha='left', va='center')
            pl.text(1.015, 0.8, '100%', color='m', transform=ax1.transAxes, ha='left', va='center')
            pl.text(1.03, 0.55, 'Atmospheric Transmission', color='m', ha='center', va='center', 
                    rotation=90, transform=ax1.transAxes, size=11)
            for yt in np.arange(0.3, 0.81, 0.1):
                xtickTrans,ytickTrans = np.array([[1,1.01],[yt,yt]])
                line = matplotlib.lines.Line2D(xtickTrans,ytickTrans,color='m',transform=ax2.transAxes)
                ax1.add_line(line)
                line.set_clip_on(False)
        else:
            atmRescaled = ylim[0] + 0.3*yrange + atm*atmRange*yrange/300.
            pl.text(1.015, 0.3, '0', color='m', transform=ax1.transAxes, ha='left', va='center')
            pl.text(1.015, 0.8, '300', color='m', transform=ax1.transAxes, ha='left', va='center')
            pl.text(1.03, 0.55, 'Sky temperature (K)', color='m', ha='center', va='center', 
                    rotation=90, transform=ax1.transAxes, size=11)
            for yt in np.arange(0.3, 0.81, 0.1):
                xtickTrans,ytickTrans = np.array([[1,1.01],[yt,yt]])
                line = matplotlib.lines.Line2D(xtickTrans,ytickTrans,color='m',transform=ax2.transAxes)
                ax1.add_line(line)
                line.set_clip_on(False)
        pl.text(1.06, 0.55, '(%.1f mm PWV, 1.5 airmass)'%pwv, color='m', ha='center', va='center', 
                rotation=90, transform=ax1.transAxes, size=11)
#        pl.plot(freqs, atmRescaled, 'w.', ms=0)  # need this to finalize the ylim value
        ylim = pl.ylim()
        yrange = ylim[1]-ylim[0]
        if (value == 'transmission'):
            atmRescaled = ylim[0] + 0.3*yrange + atm*atmRange*yrange
        else:
            atmRescaled = ylim[0] + 0.3*yrange + atm*atmRange*yrange/300.
        pl.plot(freqs, atmRescaled, 'm-')
        pl.ylim(ylim)  # restore the prior value (needed for CASA 5.0)
#    pl.draw()
    if (png == ''):
        if pngBasename:
            png = os.path.basename(img)
        else:
            png = img
        transition = transition.replace('(','_').replace(')','_').replace(' ','_').replace(',','')
        if (len(transition) > 0):
            transition = '.' + transition
        narrowString = pickNarrowString(narrow, len(avgSpectrumNansReplaced), narrowValueModified) # this is used later
        trimString = pickTrimString(trimChannels, len(avgSpectrumNansReplaced), maxTrim)
        png += '.meanSpectrum.%s.%s.%s.%.1fsigma.narrow%s.trim%s%s' % (meanSpectrumMethod, baselineModeA, baselineModeB, sigmaFindContinuum, narrowString, trimString, transition)
        if maxMemory > 0:
            png += '.%.0fGB' % (maxMemory)
        if overwrite:
            png += '.overwriteTrue.png'
        else: # change the following to '.png' after my test of July 20, 2016
            png += '.png'
    pngdir = os.path.dirname(png)
    if (len(pngdir) < 1):
        pngdir = '.'
    if (not os.access(pngdir, os.W_OK) and pngdir != '.'):
        casalogPost("No permission to write to specified directory: %s. Will try alternateDirectory." % pngdir)
        if (len(alternateDirectory) < 1):
            alternateDirectory = '.'
        png = alternateDirectory + '/' + os.path.basename(png)
        pngdir = alternateDirectory
        print "png = ", png
    if (not os.access(pngdir, os.W_OK)):
        casalogPost("No permission to write to alternateDirectory. Will not save the plot.")
    else:
        pl.savefig(png)
        casalogPost("Wrote png = %s" % (png))
    donetime = timeUtilities.time()
    casalogPost("%.1f sec elapsed in runFindContinuum" % (donetime-startTime))
#    sd = open('spectralDiff.dat','a')
#    sd.write('%f %f %s\n' % (spectralDiff[0],spectralDiff[1],os.path.basename(img)))
    return(selection, png, slope, channelWidth, nchan, useLowBaseline)
# end of runFindContinuum

def aboveBelow(avgSpectrumNansReplaced, threshold):
    """
    Given an array of values (i.e. a spectrum) and a threshold value, this function
    computes and returns 6 items: the number of channels above and below that threshold 
    and their ratio, plus the sum of the intensities of these respective channels and 
    the ratio of these sums.
    """
    aboveMedian = np.where(avgSpectrumNansReplaced > threshold)
    belowMedian = np.where(avgSpectrumNansReplaced < threshold)
    sumAboveMedian = np.sum(-threshold+avgSpectrumNansReplaced[aboveMedian])
    sumBelowMedian = np.sum(threshold-avgSpectrumNansReplaced[belowMedian])
    channelsAboveMedian = len(aboveMedian[0])
    channelsBelowMedian = len(belowMedian[0])
    channelRatio = channelsAboveMedian*1.0/channelsBelowMedian
    sumRatio = sumAboveMedian/sumBelowMedian
    return(sumAboveMedian, sumBelowMedian, sumRatio, 
           channelsAboveMedian, channelsBelowMedian, channelRatio)

def writeMeanSpectrum(meanSpectrumFile, frequency, avgspectrum, 
                      avgSpectrumNansReplaced, threshold, edgesUsed, 
                      nchan, nanmin, centralArcsec='auto', mask='', iteration=0):
    """
    Writes out the mean spectrum (and the key parameters used to create it), so that
    it can quickly be restored.  This allows the user to quickly experiment with 
    different parameters of findContinuumChannels applied to the same mean spectrum.
    Units are Hz and Jy/beam.
    Returns: None
    """
    f = open(meanSpectrumFile, 'w')
    if (iteration == 0):
        f.write('#threshold edgesUsed nchan nanmin centralArcsec=%s %s\n' % (str(centralArcsec),mask))
    else:
        f.write('#threshold edgesUsed nchan nanmin centralArcsec=auto %s %s\n' % (str(centralArcsec),mask))
    f.write('%.10f %d %d %.10f\n' % (threshold, edgesUsed, nchan, nanmin))
    f.write('#chan freq(Hz) avgSpectrum avgSpectrumNansReplaced\n')
    for i in range(len(avgspectrum)):
        f.write('%d %.1f %.10f %.10f\n' % (i, frequency[i], avgspectrum[i], avgSpectrumNansReplaced[i]))
    f.close()

def readViewerOutputFile(lines, debug=False):
    """
    Reads an ASCII spectrum file produced by the CASA viewer or by tt.ispec.
    Returns: 4 items: 2 arrays and 2 strings:
    * array for x-axis, array for y-axis 
    * x-axis units, intensity units
    """
    x = []; y = []
    pixel = ''
    xunits = 'unknown'
    intensityUnits = 'unknown'
    for line in lines:
        if (line[0] == '#'): 
            if (line.find('pixel') > 0):
                pixel = line.split('[[')[1].split(']]')[0]
            elif (line.find('xLabel') > 0):
                xunits = line.split()[-1]
                if (debug):
                    print "Read xunits = ", xunits
            elif (line.find('yLabel') > 0):
                tokens = line.split()
                if (len(tokens) == 2):
                    intensityUnits = tokens[1]
                else:
                    intensityUnits = tokens[1] + ' (' + tokens[2] + ')'
            continue
        tokens = line.split()
        if (len(tokens) < 2): 
            continue
        x.append(float(tokens[0]))
        y.append(float(tokens[1]))
    return(np.array(x), np.array(y), xunits, intensityUnits)

def readMeanSpectrumFITSFile(meanSpectrumFile, unit=0, nanBufferChannels=1):
    """
    Reads a spectrum from a FITS table, such as one output by spectralcube.
    Returns: 8 items
    * average spectrum
    * average spectrum with the NaNs replaced with the minimum value
    * threshold used (currently hardcoded to 0.0)
    * edges used (currently hardcoded to 2)
    * number of channels
    * the minimum value
    * first frequency
    * last frequency
    """
    f = pyfits.open(meanSpectrumFile)
    tbheader = f[unit].header
    tbdata = f[unit].data
    nchan = len(tbdata)
    crpix = tbheader['CRPIX1']
    crval = tbheader['CRVAL1']
    cdelt = tbheader['CDELT1']
    units = tbheader['CUNIT1']
    if (units.lower() != ('hz')):
        print "Warning: frequency units are not Hz = ", units.lower()
        return
    firstFreq = crval - (crpix-1)*cdelt
    lastFreq = firstFreq + cdelt*(nchan-1)
    avgspectrum = tbdata
    edgesUsed = 2
    threshold = 0
    avgSpectrumNansReplaced,nanmin = removeNaNs(tbdata, replaceWithMin=True, 
                                     nanBufferChannels=nanBufferChannels)
    return(avgspectrum, avgSpectrumNansReplaced, threshold,
           edgesUsed, nchan, nanmin, firstFreq, lastFreq)
    
def readPreviousMeanSpectrum(meanSpectrumFile, verbose=False, invert=False):
    """
    Read a previous calculated mean spectrum and its parameters,
    or an ASCII file created by the CASA viewer (or tt.ispec).
    Note: only the intensity column(s) are returned, not the 
       channel/frequency columns.
    """
    f = open(meanSpectrumFile, 'r')
    lines  = f.readlines()
    f.close()
    if (len(lines) < 3):
        return None
    i = 0
#    Might want to read this in someday
#    centralArcsec = '-1'
    # Detect file type:
    if (lines[0].find('title: Spectral profile') > 0):
        # CASA viewer/au.ispec output file
        print "Reading CASA viewer output file"
        freq, intensity, freqUnits, intensityUnits = readViewerOutputFile(lines)
        if (freqUnits.lower().find('ghz')>=0):
            freq *= 1e9
        elif (freqUnits.lower().find('mhz')>=0):
            freq *= 1e6
        elif (freqUnits.lower().find('[hz')<0):
            print "Spectral axis of viewer output file must be in Hz, MHz or GHz, not %s." % freqUnits
            return
        threshold = 0
        edgesUsed = 2
        return(intensity, intensity, threshold,
               edgesUsed, len(intensity), np.nanmin(intensity), freq[0], freq[-1], None, None, None)
    centralArcsec = ''
    mask = ''
    percentagePixelsNotMasked = -1
    while (lines[i][0] == '#'):
        if (lines[i].find('centralArcsec=') > 0):
            if (lines[i].find('=auto') > 0):
                centralArcsec = 'auto'
            else:
                centralArcsec = float(lines[i].split('centralArcsec=')[1].split()[0])
            token = lines[i].split()
            if (len(token) > 6):
                mask = token[6]
                if (len(token) > 7):
                    percentagePixelsNotMasked = int(token[7])
        i += 1
    token = lines[i].split()
    if len(token) == 4:
        a,b,c,d = token
        threshold = float(a)
        edgesUsed = int(b)
        nchan = int(c)
        nanmin = float(d)
    else:
        threshold = 0
        edgesUsed = 0
        nchan = 0
        nanmin = 0
    avgspectrum = []
    avgSpectrumNansReplaced = []
    freqs = []
    channels = []
    for line in lines[i+1:]:
        if (line[0] == '#'): continue
        tokens = line.split()
        if (len(tokens) == 2):
            # continue to support the old 2-column format
            freq,a = line.split()
            b = a
            nchan += 1
            freq = float(freq)
            if freq < 1e6:
                freq *= 1e6 # convert MHz to Hz
            freqs.append(freq)
        elif len(tokens) > 2:
            chan,freq,a,b = line.split()
            channels.append(int(chan))
            freqs.append(float(freq))
        else:
            print "len(tokens) = ", len(tokens)
        if invert:
            a = -float(a)
            b = -float(b)
        avgspectrum.append(float(a))
        avgSpectrumNansReplaced.append(float(b))
    avgspectrum = np.array(avgspectrum)    
    avgSpectrumNansReplaced = np.array(avgSpectrumNansReplaced)
    if invert:
        avgspectrum += abs(np.min(avgspectrum))
        avgSpectrumNansReplaced += abs(np.min(avgSpectrumNansReplaced))
        
    if (len(freqs) > 0):
        firstFreq = freqs[0]
        lastFreq = freqs[-1]
    else:
        firstFreq = 0
        lastFreq = 0
    casalogPost("Read previous mean spectrum with %d channels, (%d freqs: %f-%f)" % (len(avgspectrum),len(freqs),firstFreq,lastFreq),verbose)
    return(avgspectrum, avgSpectrumNansReplaced, threshold,
           edgesUsed, nchan, nanmin, firstFreq, lastFreq, centralArcsec,
           mask, percentagePixelsNotMasked)

def findContinuumChannels(spectrum, nBaselineChannels=16, sigmaFindContinuum=3, 
                          nanmin=None, baselineMode='min', trimChannels='auto',
                          narrow='auto', verbose=False, maxTrim=maxTrimDefault, 
                          maxTrimFraction=1.0, separator=';', fitResult=None,
                          maxGroupsForMaxTrimAdjustment=3, lowHighBaselineThreshold=1.5,
                          lineSNRThreshold=20, negativeThresholdFactor=1.15, 
                          dropBaselineChannels=2.0, madRatioUpperLimit=1.5, madRatioLowerLimit=1.15):
    """
    Trys to find continuum channels in a spectrum, based on a threshold or
    some number of edge channels and their median and standard deviation.
    Inputs:
    spectrum: a one-dimensional array of intensity values
    nBaselineChannels: number of channels over which to compute standard deviation/MAD
    sigmaFindContinuum: value to multiply the standard deviation by then add 
      to median to get threshold.  Default = 3.  
    narrow: the minimum number of channels in a contiguous block to accept 
            if 0<narrow<1, then it is interpreted as the fraction of all
                           channels within identified blocks
    nanmin: the value that NaNs were replaced by in previous steps
    baselineMode: 'min' or 'edge',  'edge' will use nBaselineChannels/2 from each edge
    trimChannels: if integer, use that number of channels.  If float between
      0 and 1, then use that fraction of channels in each contiguous list
      (rounding up). If 'auto', then use 0.1 but not more than maxTrim channels.
    maxTrim: if trimChannels='auto', this is the max channels to trim per group for TDM spws; it is automatically scaled upward for FDM spws.
    maxTrimFraction: in trimChannels='auto', the max fraction of channels to trim per group
    separator: the character to use to separate groups of channels in the string returned
    negativeThresholdFactor: scale the nominal negative threshold by this factor (to adjust 
        sensitivity to absorption features: smaller values=more sensitive)
    dropBaselineChannels: percentage of extreme values to drop in baseline mode 'min'
    madRatioUpperLimit, madRatioLowerLimit: if ratio of MADs is between these values, then
        apply dropBaselineChannels when defining the MAD of the baseline range

    Returns:
    1  list of channels to use (separated by the specified separator)
    2  list converted to ms channel selection syntax
    3  positive threshold used
    4  median of the baseline-defining channels
    5  number of groups found
    6  correctionFactor used
    7  inferred true median
    8  scaled MAD of the baseline-defining channels
    9  correction factor applied to get the inferred true median
    10 negative threshold used
    11 lineStrength factor
    12 singleChannelPeaksAboveSFC: how many cases where only a single channel exceeds the threshold 
    13 allGroupsAboveSFC
    14 spectralDiff (percentage of median)
    15 value of trimChannels parameter
    16 Boolean describing whether the low values were used as the baseline
    17 value of the narrow parameter
    18 tuple containing the channel numbers of the baseline channels, and their respective y-axis values
    19 value of madRatio
    """
    if (fitResult is not None):
        myx = np.arange(len(spectrum), dtype=np.float64)
        if (len(fitResult) > 2):
            myx -= fitResult[3]
            originalSpectrum = spectrum + (fitResult[0]*myx**2 + fitResult[1]*myx + fitResult[2]) - nanmean(spectrum)
#            print "fitResult = ", fitResult
            casalog.post("min/max spectrum = %f, %f" % (np.min(spectrum), np.max(spectrum)))
            casalog.post("min/max originalSpectrum = %f, %f" % (np.min(originalSpectrum), np.max(originalSpectrum)))
        else:
            originalSpectrum = spectrum + fitResult[0]*myx
    else:
        originalSpectrum = spectrum
    if (narrow == 'auto'):
        narrow = pickNarrow(len(spectrum))
        autoNarrow = True
    else:
        autoNarrow = False
    npts = len(spectrum)
    percentile = 100.0*nBaselineChannels/npts
    correctionFactor = sigmaCorrectionFactor(baselineMode, npts, percentile)
    sigmaEffective = sigmaFindContinuum*correctionFactor
    if (fitResult is not None):
        if (len(fitResult) > 2):
            casalogPost("****** starting findContinuumChannels (polynomial=%g*(x-%.2f)**2+%g*(x-%.2f)+%g) ***********" % (fitResult[0], fitResult[3], fitResult[1], fitResult[3], fitResult[2]))
        else:
            casalogPost("****** starting findContinuumChannels (slope=%g) ***********" % (fitResult[0]))
    else:
        casalogPost("****** starting findContinuumChannels ***********")
    casalogPost("Using sigmaFindContinuum=%.2f, sigmaEffective=%.1f, percentile=%.0f for mode=%s, channels=%d/%d" % (sigmaFindContinuum, sigmaEffective, percentile, baselineMode, nBaselineChannels, len(spectrum)))
    if (baselineMode == 'edge'):
        # pick n channels on both edges
        lowerChannels = spectrum[:nBaselineChannels/2]
        upperChannels = spectrum[-nBaselineChannels/2:]
        allBaselineChannels = list(lowerChannels) + list(upperChannels)
        allBaselineXChannels = range(0,nBaselineChannels/2) + range(len(spectrum)-nBaselineChannels/2,len(spectrum))
        if (np.std(lowerChannels) == 0):
            mad = MAD(upperChannels)
            median = nanmedian(upperChannels)
            casalogPost("edge method: Dropping lower channels from median and std calculations")
        elif (np.std(upperChannels) == 0):
            mad = MAD(lowerChannels)
            median = nanmedian(lowerChannels)
            casalogPost("edge method: Dropping upper channels from median and std calculations")
        else:
            mad = MAD(allBaselineChannels)
            median = nanmedian(allBaselineChannels)
        useLowBaseline = True
    else:
        # Pick the n channels with the n lowest values (or highest if those have smallest MAD), but
        # ignore edge channels inward for as long they are identical to themselves (i.e. avoid the
        # effectd of TDM edge flagging.)
        myspectrum = spectrum
        if (len(originalSpectrum) > 10 and len(originalSpectrum) <= 128):
            # Could open this up to all data except it makes a 4096-channel 
            # spw worse: G0.25+0.02__sci.spw16, and it picks up some
            # continuum in self-absorbed area in Cha-MMs1_CS in 200-channel spw
            myspectrum = spectrum[np.where((originalSpectrum != originalSpectrum[0]) * (originalSpectrum != originalSpectrum[-1]))]
            casalogPost('Avoided %d edge channels when computing min channels' % (len(spectrum)-len(myspectrum)))
        idx = np.argsort(myspectrum)
        allBaselineChannels = myspectrum[idx[:nBaselineChannels]] 
        allBaselineXChannels = idx[:nBaselineChannels]
        allBaselineOriginalChannels = originalSpectrum[idx[:nBaselineChannels]]
        highestChannels = myspectrum[idx[-nBaselineChannels:]]  
        medianOfAllChannels = nanmedian(myspectrum)
        mad0 = MAD(allBaselineChannels)
        mad1 = MAD(highestChannels)

        # Introduce the lowHighBaselineThreshold factor on Aug 31, 2016 for CAS-8938
        if (mad0 > lowHighBaselineThreshold*mad1):
            casalogPost("Using highest %d channels as baseline because %g > %.1f*%g" % (nBaselineChannels,mad0,lowHighBaselineThreshold,mad1))
            allBaselineChannels = highestChannels[::-1] # reversed it so that first channel is highest value
            mad0 = MAD(allBaselineChannels)
            useLowBaseline = False
        else:
            if verbose:
                casalogPost("Using lowest %d channels as baseline: %s %s" % (nBaselineChannels, idx[:nBaselineChannels], myspectrum[idx[:10]]))
            casalogPost("Using lowest %d channels as baseline because %g <= %.1f*%g" % (nBaselineChannels,mad0,lowHighBaselineThreshold,mad1))
            useLowBaseline = True

        casalogPost("Median of all channels = %f,  MAD of selected baseline channels = %f" % (medianOfAllChannels,mad0))
        madRatio = None
        if dropBaselineChannels > 0:
            dropExtremeChannels = int(len(idx)*dropBaselineChannels)/100
            if dropExtremeChannels > 0:
                allBaselineChannelsDropExtremeChannels = myspectrum[idx[dropExtremeChannels:nBaselineChannels+dropExtremeChannels]] 
                mad0_dropExtremeChannels = MAD(allBaselineChannelsDropExtremeChannels)
                if mad0_dropExtremeChannels > 0:
                    # prevent division by zero error
                    madRatio = mad0/mad0_dropExtremeChannels
                    if madRatioLowerLimit < madRatio < madRatioUpperLimit:
                        # more than 1.2 means there was a significant improvement; more than 1.5 means something unexpected about the statistics
                        casalogPost("****** Dropping most extreme %d = %.1f%% of channels when computing the MAD, since it reduces the mad by a factor of x=%.2f (%.2f<x<%.2f)" % (dropExtremeChannels, dropBaselineChannels, madRatio, madRatioLowerLimit, madRatioUpperLimit))
                        allBaselineChannels = allBaselineChannelsDropExtremeChannels
                        allBaselineXChannels = idx[dropExtremeChannels:nBaselineChannels+dropExtremeChannels]
                        allBaselineOriginalChannels = originalSpectrum[idx[dropExtremeChannels:nBaselineChannels+dropExtremeChannels]]
                    else:
                        casalogPost("**** Not dropping most extreme channels when computing the MAD, since the change in MAD of %.2f is not within %.2f<x<%.2f" % (madRatio, madRatioLowerLimit, madRatioUpperLimit))
            

        casalogPost("min method: computing MAD and median of %d channels used as the baseline" % (len(allBaselineChannels)))
        mad = MAD(allBaselineChannels)
        madOriginal = MAD(allBaselineOriginalChannels)
        casalogPost("MAD of all baseline channels = %f" % (mad))
        if (fitResult is not None):
            casalogPost("MAD of original baseline channels (before removal of fit) = %f" % (madOriginal))
        if (mad < 1e-17 or madOriginal < 1e-17): 
            casalogPost("min method: avoiding blocks of identical-valued channels")
            if (len(originalSpectrum) > 10):
                myspectrum = spectrum[np.where((originalSpectrum != originalSpectrum[0]) * (originalSpectrum != originalSpectrum[-1]))]
            else: # original logic, prior to linear fit removal
                myspectrum = spectrum[np.where(spectrum != allBaselineChannels[0])]
            allBaselineChannels = myspectrum[np.argsort(myspectrum)[:nBaselineChannels]] 
            casalogPost("            computing MAD and median of %d channels used as the baseline" % (len(allBaselineChannels)))
            mad = MAD(allBaselineChannels)
        mad = MAD(allBaselineChannels)
        median = nanmedian(allBaselineChannels)
        casalogPost("min method: median intensity of %d channels used as the baseline: %f" % (len(allBaselineChannels), median))
    # signalRatio will be 1.0 if no lines present and 0.25 if half the channels have lines, etc.
    signalRatio = (1.0 - 1.0*len(np.where(np.abs(spectrum-median)>(sigmaEffective*mad*2.0))[0]) / len(spectrum))**2
    originalMedian = np.median(originalSpectrum)
    # Should not divide by post-baseline-fit median since it may be close to 0
    spectralDiff = 100*np.median(np.abs(np.diff(spectrum)))/originalMedian
    spectralDiff2 = 100*np.median(np.abs(np.diff(spectrum,n=2)))/originalMedian
    casalogPost("signalRatio=%f, spectralDiff = %f and spectralDiff2=%f percent of the median" % (signalRatio, spectralDiff,spectralDiff2))
    lineStrengthFactor = 1.0/signalRatio
    if (spectralDiff2 < 0.65 and npts > 192 and signalRatio<0.95):
        # This appears to be a channel-averaged FDM spectrum with lots of real line emission.
        # So, don't allow the median to be raised, and reduce the mad to lower the threshold.
        # page 15: G11.92_B7.ms_spw3 yields spectralDiff2=0.6027
        # We can also get into here if there is a large slope in the mean spectrum, so we
        # counter that by removing a linear slope before evaluating lineSNR.
        casalogPost('The spectral difference (n=2) is rather small, so set signalRatio=0 to reduce the baseline level.',debug=True)
        signalRatio = 0
        if True:
            # note: this slope removal differs from the one in runFindContinuum because
            # it always acts upon the whole spectrum, not just the potential baseline windows.
            print "Removing linear slope for purposes of computing lineSNR."
            x = np.arange(len(spectrum))
            slope, intercept = linfit(x, spectrum, MAD(spectrum))
            newspectrum = spectrum - x*slope
            newmad = MAD(newspectrum)
            lineSNR = (np.max(newspectrum)-np.median(newspectrum))/newmad
        else:
            lineSNR = (np.max(spectrum)-median)/mad
        casalogPost('lineSNR = %f' % lineSNR)
        if (lineSNR > lineSNRThreshold):
            casalogPost('The lineSNR > %d, so scaling the mad by 1/3 to reduce the threshold.' % lineSNRThreshold, debug=True)
            mad *= 0.33
            if (trimChannels == 'auto'): 
                trimChannels = 6
                casalogPost('Setting trimChannels to %d.' % (trimChannels))
    else:
        casalogPost('Not reducing mad by 1/3: npts=%d, signalRatio=%.2f, spectralDiff2=%.2f' % (npts,signalRatio,spectralDiff2),debug=True)
        
    medianTrue = medianCorrected(baselineMode, percentile, median, mad, 
                                 signalRatio, useLowBaseline)
    peakFeatureSigma = (np.max(spectrum)-medianTrue)/mad 
    if False:
        # experimental heuristic
        minChannelsForIncreasingSigma = 360
        maxChannelsForIncreasingSigma = 1440
        maxChannelsForDecreasingSigma = 2880
        maxSigma = 25
        if 12.5 < peakFeatureSigma < maxSigma and npts > 360:
            # No string features, then adjust sigmaEffective up or down depending on amount of channel averaging
            if (npts < 720 and 20 < peakFeatureSigma < maxSigma) or npts >= 1440:
                # nominally for the 480 and 960 channel cases
                sigmaEffective = min(sigmaEffective+4, sigmaEffective*1.25)
                casalogPost('Increasing sigmaEffective to %.2f since mad*12.5<peakFeature<%.1f*mad and channels ~480 or ~960' % (sigmaEffective,maxSigma))
            elif 1440 < npts < 2880:
                # nominally for the 1920 case
                sigmaEffective = max(sigmaEffective-4, sigmaEffective*0.75)
                casalogPost('Decreasing sigmaEffective to %.2f since mad*12.5<peakFeature<%.1f*mad and channels ~ 1920' % (sigmaEffective,maxSigma))
    threshold = sigmaEffective*mad + medianTrue
    # Use a (default=15%) lower negative threshold to help prevent false identification of absorption features.
    negativeThreshold = -negativeThresholdFactor*sigmaEffective*mad + medianTrue
    casalogPost("MAD = %f, median = %f, trueMedian=%f, signalRatio=%f" % (mad, median, medianTrue, signalRatio))
    casalogPost("findContinuumChannels: computed threshold = %f, medianTrue=%f" % (threshold, medianTrue))
    channels = np.where(spectrum < threshold)[0]
    if (negativeThreshold is not None):
        channels2 = np.where(spectrum > negativeThreshold)[0]
        channels = np.intersect1d(channels,channels2)

    # for CAS-8059: remove channels that are equal to the minimum if all 
    # channels from it toward the nearest edge are also equal to the minimum: 
    channels = list(channels)
    if (abs(originalSpectrum[np.min(channels)] - np.min(originalSpectrum)) < abs(1e-10*np.min(originalSpectrum))):
        lastmin = np.min(channels)
        channels.remove(lastmin)
        removed = 1
        casalogPost("Checking channels %d-%d" % (np.min(channels),np.max(channels)))
        for c in range(np.min(channels),np.max(channels)):
            mydiff = abs(originalSpectrum[c] - np.min(originalSpectrum))
            mycrit = abs(1e-10*np.min(originalSpectrum))
            if (mydiff > mycrit):
                break
            if c in channels:
                channels.remove(c)
                removed += 1
        casalogPost("Removed %d channels on low channel edge that were at the minimum." % (removed))
    # Now come in from the upper side
    if (abs(originalSpectrum[np.max(channels)] - np.min(originalSpectrum)) < abs(1e-10*np.min(originalSpectrum))):
        lastmin = np.max(channels)
        channels.remove(lastmin)
        removed = 1
        casalog.post("Checking channels %d-%d" % (np.max(channels),np.min(channels)))
        for c in range(np.max(channels),np.min(channels)-1,-1):
            mydiff = abs(originalSpectrum[c] - np.min(originalSpectrum))
            mycrit = abs(1e-10*np.min(originalSpectrum))
            if (mydiff > mycrit):
                break
            if c in channels:
                channels.remove(c)
                removed += 1
        casalogPost("Removed %d channels on high channel edge that were at the minimum." % (removed))
    peakChannels = np.where(spectrum > threshold)[0]
    peakChannelsLists = splitListIntoContiguousLists(peakChannels)
    widthOfWidestFeature = maxLengthOfLists(peakChannelsLists)
    casalogPost("Width of widest feature = %d (length spectrum = %d)" % (widthOfWidestFeature, len(spectrum)))
    # C4R2 had signalRatio < 0.6 and spectralDiff2 < 1.2 but this yielded only 1 channel 
    # of continuum on NGC6334I spw25 when memory expanded to 256GB.
    if (signalRatio > 0 and signalRatio < 0.925 and spectralDiff2 < 1.3 and 
        len(spectrum) > 1000  and trimChannels=='auto' and 
        widthOfWidestFeature < len(spectrum)/8):
        # This is meant to prevent rich hot cores from returning only 1
        # or 2 channels of continuum.  signalRatio>0 is to avoid conflict
        # with the earlier heuristic above where it is set to zero.
        trimChannels = 13
        if autoNarrow:
            narrow = 2
        casalogPost('Setting trimChannels=%d, narrow=%s since many lines appear to be present (signalRatio=%f).' % (trimChannels,str(narrow), signalRatio))
    else:
        casalogPost('Not changing trimChannels from %s: signalRatio=%f, spectralDiff2=%f' % (str(trimChannels), signalRatio, spectralDiff2))
        

    peakMultiChannelsLists = splitListIntoContiguousListsAndRejectNarrow(peakChannels, narrow=2)
    allGroupsAboveSFC = len(peakChannelsLists)
    singleChannelPeaksAboveSFC = allGroupsAboveSFC - len(peakMultiChannelsLists)
    selection = convertChannelListIntoSelection(channels)
    casalogPost("Found %d potential continuum channels: %s" % (len(channels), str(selection)))
    if (len(channels) == 0):
        selection = ''
        groups = 0
    else:
        channels = splitListIntoContiguousListsAndRejectZeroStd(channels, spectrum, nanmin, verbose=verbose)
        if verbose: 
            print "channels = ", channels
        selection = convertChannelListIntoSelection(channels,separator=separator)
        groups = len(selection.split(separator))
        casalogPost("Found %d channels after rejecting zero std: %s" % (len(channels), str(selection)))
        if (len(channels) == 0):
            selection = ''
        else:
            if verbose:
                casalogPost("Calling splitListIntoContiguousListsAndTrim(channels=%s, trimChannels=%s, maxTrim=%d, maxTrimFraction=%f)" % (str(channels), str(trimChannels), maxTrim, maxTrimFraction))
            else:
                casalogPost("Calling splitListIntoContiguousListsAndTrim(trimChannels=%s, maxTrim=%d, maxTrimFraction=%f)" % (str(trimChannels), maxTrim, maxTrimFraction))
            channels = splitListIntoContiguousListsAndTrim(channels, 
                         trimChannels, maxTrim, maxTrimFraction, verbose)
            if verbose:
                print "channels = ", channels
            selection = convertChannelListIntoSelection(channels)
            groups = len(selection.split(separator))
            if (groups > maxGroupsForMaxTrimAdjustment and trimChannels=='auto'
                and maxTrim>maxTrimDefault):
                maxTrim = maxTrimDefault
                casalogPost("Restoring maxTrim=%d because groups now > %d" % (maxTrim,maxGroupsForMaxTrimAdjustment))
                if verbose:
                    casalogPost("Calling splitListIntoContiguousListsAndTrim(channels=%s, trimChannels=%s, maxTrim=%d, maxTrimFraction=%f)" % (str(channels), str(trimChannels), maxTrim, maxTrimFraction))
                channels = splitListIntoContiguousListsAndTrim(channels, 
                             trimChannels, maxTrim, maxTrimFraction, verbose)
                if verbose:
                    print "channels = ", channels
                selection = convertChannelListIntoSelection(channels)
                groups = len(selection.split(separator))

            if verbose:
                print "Found %d groups of channels = " % (groups), channels
            if (groups > 1):
                if verbose:
                    casalogPost("Calling splitListIntoContiguousListsAndRejectNarrow(channels=%s, narrow=%s)" % (str(channels), str(narrow)))
                else:
                    casalogPost("Calling splitListIntoContiguousListsAndRejectNarrow(narrow=%s)" % (str(narrow)))
                trialChannels = splitListIntoContiguousListsAndRejectNarrow(channels, narrow)
                if (len(trialChannels) > 0):
                    channels = trialChannels
                    casalogPost("Found %d channels after trimming %s channels and rejecting narrow groups." % (len(channels),str(trimChannels)))
                    selection = convertChannelListIntoSelection(channels)
                    groups = len(selection.split(separator))
            else:
                casalogPost("Not rejecting narrow groups since there is only %d group!" % (groups))
    casalogPost("Found %d continuum channels in %d groups: %s" % (len(channels), groups, selection))
    if True:
        channels = rejectNarrowInnerWindowsChannels(channels)
        selection = convertChannelListIntoSelection(channels)
        groups = len(selection.split(separator))
        casalogPost("Final: found %d continuum channels (sFC=%.2f) in %d groups: %s" % (len(channels), sigmaFindContinuum, groups, selection))
    return(channels, selection, threshold, median, groups, correctionFactor, 
           medianTrue, mad, computeMedianCorrectionFactor(baselineMode, percentile)*signalRatio,
           negativeThreshold, lineStrengthFactor, singleChannelPeaksAboveSFC, 
           allGroupsAboveSFC, [spectralDiff, spectralDiff2], trimChannels, 
           useLowBaseline, narrow, [allBaselineXChannels,allBaselineChannels], madRatio)

def rejectNarrowInnerWindowsChannels(channels):
    """
    If there are 3-15 groups of channels, then remove any inner window that is narrower than
    both edge windows.
    Returns: a list of channels
    """
    mylists = splitListIntoContiguousLists(channels)
    groups = len(mylists)
#    if (groups > 2 and groups < 8):  C4R2
    if (groups > 2 and groups < 16):
        channels = []
        lenFirstGroup = len(mylists[0])
        lenLastGroup = len(mylists[-1])
        channels += mylists[0]
        if (groups < 8):
            widthFactor = 1
        else:
            # this is to prevent unnecessarily trimming narrow windows, e.g. in a hot core case
            widthFactor = 0.2
        for group in range(1,groups):
#            if (len(mylists[group]) >= np.mean([lenFirstGroup,lenLastGroup])*widthFactor): #  or len(mylists[group]) >= lenLastGroup*widthFactor):
            if (len(mylists[group]) >= np.min([lenFirstGroup,lenLastGroup])*widthFactor): 
                channels += mylists[group]
    return(channels)

def splitListIntoContiguousListsAndRejectNarrow(channels, narrow=3):
    """
    Split a list of channels into contiguous lists, and reject those that
    have a small number of channels.
    narrow: if >=1, then interpret it as the minimum number of channels that
                  a group must have in order to survive
            if <1, then interpret it as the minimum fraction of channels that
                  a group must have relative to the total number of channels
    Returns: a new single list (as an array)
    Example:  [1,2,3,4,6,7,9,10,11] --> [ 1,  2,  3,  4,  9, 10, 11]
    """
    length = len(channels)
    mylists = splitListIntoContiguousLists(channels)
    channels = []
    for mylist in mylists:
        if (narrow < 1):
            if (len(mylist) <= fraction*length):
                continue
        elif (len(mylist) < narrow):
            continue
        channels += mylist
    return(np.array(channels))

def splitListIntoContiguousListsAndTrim(channels, trimChannels=0.1, 
                                        maxTrim=maxTrimDefault, 
                                        maxTrimFraction=1.0, verbose=False):
    """
    Split a list of channels into contiguous lists, and trim some number
    of channels from each edge.
    channels: a list of channels selected for potential continuum
    trimChannels: if integer, use that number of channels.  If float between
        0 and 1, then use that fraction of channels in each contiguous list
        (rounding up). If 'auto', then use 0.1 but not more than maxTrim 
        channels and not more than maxTrimFraction of channels.
    maxTrim: used in 'auto' mode
    Returns: a new single list
    """
    if type(trimChannels) != str:
        if (trimChannels <= 0):
            return(np.array(channels))
    length = len(channels)
    trimChannelsMode = trimChannels
    if (trimChannels == 'auto'):
        trimChannels = pickAutoTrimChannels(length, maxTrim)
    mylists = splitListIntoContiguousLists(channels)
    channels = []
    trimLimitForEdgeRegion = 3
    for i,mylist in enumerate(mylists):
        trimChan = trimChannels
        if verbose:
            print "trimChan=%d, Checking list = " % (trimChan), mylist
        if (trimChannels < 1):
            trimChan = int(np.ceil(len(mylist)*trimChannels))
            if verbose:
                print "since trimChannels=%s<1; reset trimChan=%d" % (str(trimChannels),trimChan)
        if (trimChannelsMode == 'auto' and 1.0*trimChan/len(mylist) > maxTrimFraction):
            trimChan = int(np.floor(maxTrimFraction*len(mylist)))
        if verbose:
            print "trimChan for this window = %d" % (trimChan)
        if (len(mylist) < 1+trimChan*2):
            if (len(mylists) == 1):
                # If there was only one list of 1 or 2 channels, then don't trim it away!
                channels += mylist[:1]
            continue
        # Limit the trimming of the edge closest to the edge of the spw to 3 channels,
        # in order to preserve bandwidth.
        if (i==0 and trimChan > trimLimitForEdgeRegion):
            if (len(mylists)==1):
                # It is the only window so limit the trim on the far edge too
                channels += mylist[trimLimitForEdgeRegion:-trimLimitForEdgeRegion]
            else:
                channels += mylist[trimLimitForEdgeRegion:-trimChan]
        elif (i==len(mylists)-1 and trimChan > trimLimitForEdgeRegion):
            channels += mylist[trimChan:-trimLimitForEdgeRegion]
        else:
            # It is not an edge window, or it is an edge window and trimChan<=3
            channels += mylist[trimChan:-trimChan]
    return(np.array(channels))

def maxLengthOfLists(lists):
    """
    Returns: an integer that is the length of the longest list
    """
    maxLength = 0
    for a in lists:
        if (len(a) > maxLength):
            maxLength = len(a)
    return maxLength

def roundFigures(value, digits):
    """
    This function rounds a floating point value to a number of significant figures.
    value: value to be rounded (between 1e-20 and 1e+20)
    digits: number of significant digits, both before or after decimal point
    Returns: a floating point value
    """
    if (value != 0.0):
        if (np.log10(np.abs(value)) % 1 < np.log10(5)):
            digits -= 1
    for r in range(-20,20):
        if (round(value,r) != 0.0):
            value = round(value,r+digits)
            break
    return(value)

def pickAutoTrimChannels(length, maxTrim):
    """
    Automatic choice of number of trimChannels as a function of the number 
    of channels in an spw.
    Returns: an integer
    """
    trimChannels = 0.1
    if (length*trimChannels > maxTrim):
        casalogPost("pickAutoTrimChannels(): set trimChannels = %d because %.0f > %d" % (maxTrim,length*trimChannels,maxTrim))
        trimChannels = maxTrim
    return(trimChannels)

def pickTrimString(trimChannels, length, maxTrim):
    """
    Generate a string describing the setting of the trimChannels parameter.
    Returns: a string
    """
    if (trimChannels=='auto'):
        trimString = 'auto_max=%g' % pickAutoTrimChannels(length, maxTrim)
    else:
        trimString = '%g' % trimChannels
    return(trimString)

def pickNarrowString(narrow, length, narrowValueModified=None):
    """
    Generate a string describing the setting of the narrow parameter.
    Returns: a string
    """
    if (narrow=='auto'):
        myNarrow = pickNarrow(length)
        if (narrowValueModified is None or myNarrow == narrowValueModified):
            narrowString = 'auto=%g' % myNarrow
        else:
            narrowString = 'auto=%g' % narrowValueModified
    else:
        narrowString = '%g' % narrow
    return(narrowString)

def pickNarrow(length):
    """
    Automatically picks a setting for the narrow parameter based on the number 
    of channels in the spectrum.  
    Returns: an integer
    This formula results in:
    length: 64,128,240,480,960,1920,3840,7680:
    return:  2,  3,  3,  3,  3,   4,   4,   4  (ceil(log10))
             2,  2,  2,  3,  3,   3,   4,   4  (round(log10))
             1,  2,  2,  2,  2,   3,   3,   3  (floor(log10))
             5,  5,  6,  7,  7,   8,   9,   9  (ceil(log))
             4,  5,  5,  6,  7,   8,   8,   9  (round(log))
             4,  4,  5,  6,  6,   7,   8,   8  (floor(log))
    """
    return(int(np.ceil(np.log10(length))))

def sigmaCorrectionFactor(baselineMode, npts, percentile):
    """
    Computes the correction factor for the fact that the measured rms (or MAD) 
    on the N%ile lowest points of a datastream will be less than the true rms 
    (or MAD) of all points.  
    Returns: a value between 0 and 1, which will be used to reduce the 
             estimate of the population sigma based on the sample sigma
    """
    edgeValue = (npts/128.)**0.08
    if (baselineMode == 'edge'):
        return(edgeValue)
    value = edgeValue*2.8*(percentile/10.)**-0.25
    casalogPost("sigmaCorrectionFactor using percentile = %g to get sCF=%g" % (percentile, value), debug=True)
    return(value)

def medianCorrected(baselineMode, percentile, median, mad, signalRatio, 
                    useLowBaseline):
    """
    Computes the true median of a datastream from the observed median and MAD 
    of the lowest Nth percentile points.
    signalRatio: when there is a lot of signal, we need to reduce the 
                 correction factor because it is less like Gaussian noise
                 It is 1.0 if no lines are present, 0.25 if half the channels 
                 have signal, etc.
    """
    if useLowBaseline:
        corrected = median + computeMedianCorrectionFactor(baselineMode, percentile)*mad*signalRatio
    else:
        corrected = median - computeMedianCorrectionFactor(baselineMode, percentile)*mad*signalRatio
    return(corrected)

def computeMedianCorrectionFactor(baselineMode, percentile):
    """
    Computes the effect due to the fact that taking the median of the
    N%ile lowest points of a Gaussian noise datastream will be lower than the true 
    median of all the points.  This is (TrueMedian-ObservedMedian)/ObservedMAD
    """
    if (baselineMode == 'edge'):
        return(0)
    return(6.3*(5.0/percentile)**0.5)

def headerToArcsec(mydict, unit=None):
    """
    Converts an angle quantity dictionary to the angle
    in arcsec.
    """
    if (unit is None):
        value = mydict['value']
    else:
        value = mydict
        mydict = {'value': mydict, 'unit': unit}
    if (mydict['unit'].find('rad') >= 0):
        value = 3600*np.degrees(value)
    elif (mydict['unit'].find('deg') >= 0):
        value *= 3600
    return(value)

def getImageInfo(img, header=''):
    """
    Extract the beam and pixel information from a CASA image.
    Returns: bmaj, bmin, bpa, cdelt1, cdelt2, naxis1, naxis2, frequency
             Angles are in arcseconds (bpa in degrees)
             Frequency is in GHz and is the central frequency
    -Todd Hunter
    """
    if (os.path.exists(img) == False):
        print "image not found: ", img
        return
    # Assume this is a CASA image
    if (header == ''):
        try:
            print "imhead",
            header = imhead(img, mode = 'list')  # This will work for most CASA builds
        except:
            print "imhead",
            header = imhead(img)  # needed to prevent crash in early CASA 4.6 builds (see CAS-8214)
            print "imhead",
            header = imhead(img, mode = 'list')
        if (header is None):
            print "imhead returned NoneType. This image header is not sufficiently standard."
            return
    if ('beammajor' in header.keys()):
        bmaj = header['beammajor']
        bmin = header['beamminor']
        bpa = header['beampa']
    elif ('perplanebeams' in header.keys()):
        beammajor = []
        beamminor = []
        beampa = []
        for beamchan in range(header['perplanebeams']['nChannels']):
            beamdict = header['perplanebeams']['*'+str(beamchan)]
            beammajor.append(beamdict['major']['value'])
            beamminor.append(beamdict['minor']['value'])
            beampa.append(beamdict['positionangle']['value'])
        bmaj = np.median(beammajor)
        bmin = np.median(beamminor)
        sinbpa = np.sin(np.radians(np.array(beampa)))
        cosbpa = np.cos(np.radians(np.array(beampa)))
        bpa = np.degrees(np.median(np.arctan2(np.median(sinbpa), np.median(cosbpa))))
    else:
        bmaj = 0
        bmin = 0
        bpa = 0
    naxis1 = header['shape'][0]
    naxis2 = header['shape'][1]
    cdelt1 = header['cdelt1']
    cdelt2 = header['cdelt2']
    if (header['cunit1'].find('rad') >= 0):
        # convert from rad to arcsec
        cdelt1 *= 3600*180/np.pi
    elif (header['cunit1'].find('deg') >= 0):
        # convert from deg to arcsec
        cdelt1 *= 3600
    if (header['cunit2'].find('rad') >= 0):
        cdelt2 *= 3600*180/np.pi
        # convert from rad to arcsec
    elif (header['cunit2'].find('deg') >= 0):
        # convert from deg to arcsec
        cdelt2 *= 3600
    if (type(bmaj) == dict):
        # casa >= 4.1.0  (previously these were floats)
        bmaj = headerToArcsec(bmaj)
        bmin = headerToArcsec(bmin)
        bpa = headerToArcsec(bpa)/3600.
    ghz = 0
    if ('ctype4' in header.keys()):
        if (header['ctype4'] == 'Frequency'):
            imgfreq = header['crval4']
            cdelt = header['cdelt4']
            crpix = header['crpix4']
            npix = header['shape'][3]
            ghz = imgfreq*1e-9
    if (ghz == 0):
        if ('ctype3' in header.keys()):
            if (header['ctype3'] == 'Frequency'):
                imgfreq = header['crval3']
                cdelt = header['cdelt3']
                crpix = header['crpix3']
                npix = header['shape'][2]
                ghz = imgfreq*1e-9
    return([bmaj,bmin,bpa,cdelt1,cdelt2,naxis1,naxis2,ghz], header)
                                                                
def numberOfChannelsInCube(img, returnFreqs=False, returnChannelWidth=False, 
                           verbose=False, header=None, faster=False):
    """
    Finds the number of channels in a CASA or FITS image cube.
    returnFreqs: if True, then also return the frequency of the
           first and last channel (in Hz)
    returnChannelWidth: if True, then also return the channel width (in Hz)
    verbose: if True, then print the frequencies of first and last channel
    -Todd Hunter
    """
    if (header is None):
        print "imhead", # the comma prevents the newline so that ...10...20 will be on same line
        if faster:
            header = imheadlist(img, omitBeam=True)
        else:
            header = imhead(img,mode='list')
    if (header is None):
        print "imhead failed -- this may not be a CASA or FITS image cube."
        return
    nchan = 1
    for axis in range(3,5):
        if ('ctype'+str(axis) in header.keys()):
            if (header['ctype'+str(axis)] in ['Frequency','Velocity']):
                nchan = header['shape'][axis-1]
                break
    firstFreq = 0
    lastFreq = 0
    if ('ctype4' not in header.keys()):
        print "There is no fourth axis in this image."
    elif (header['ctype4'].lower().find('freq') >= 0):
        crpix = header['crpix4']
        crval = header['crval4']
        cdelt = header['cdelt4']
        firstFreq = crval + (0-crpix)*cdelt
        lastFreq = crval + (nchan-1-crpix)*cdelt
        if (verbose):
            print "Channel  0 = %.0f Hz" % (firstFreq)
            print "Channel %d = %.0f Hz" % (nchan-1,lastFreq)
    elif (header['ctype3'].lower().find('freq') >= 0):
        crpix = header['crpix3']
        crval = header['crval3']
        cdelt = header['cdelt3']
        firstFreq = crval + (0-crpix)*cdelt
        lastFreq = crval + (nchan-1-crpix)*cdelt
        if (verbose):
            print "Channel  0 = %.0f Hz" % (firstFreq)
            print "Channel %d = %.0f Hz" % (nchan-1,lastFreq)
    else:
        casalogPost("Neither the third or fourth axis is frequency.")
    if (returnFreqs):
        if (returnChannelWidth):
            return(nchan,firstFreq,lastFreq,cdelt)
        else:
            return(nchan,firstFreq,lastFreq)
    else:
        if (returnChannelWidth):
            return(nchan,cdelt)
        else:
            return(nchan)

def nanmean(a, axis=0):
    """
    Takes the mean of an array, ignoring the nan entries
    """
    if (map(int, np.__version__.split('.')[:3]) < [1,8,1]):
        return(scipy_nanmean(a,axis)) 
#        length = len(np.array(a)[np.where(np.isnan(a)==False)])
#        return(np.nansum(a,axis)/length)
    else:
        return(np.nanmean(a,axis))

def _nanmedian(arr1d, preop=None):  # This only works on 1d arrays
    """Private function for rank a arrays. Compute the median ignoring Nan.

    Parameters
    ----------
    arr1d : ndarray
        Input array, of rank 1.

    Results
    -------
    m : float
        The median.
    """
    x = arr1d.copy()
    c = np.isnan(x)
    s = np.where(c)[0]
    if s.size == x.size:
        warnings.warn("All-NaN slice encountered", RuntimeWarning)
        return np.nan
    elif s.size != 0:
        # select non-nans at end of array
        enonan = x[-s.size:][~c[-s.size:]]
        # fill nans in beginning of array with non-nans of end
        x[s[:enonan.size]] = enonan
        # slice nans away
        x = x[:-s.size]
    if preop:
        x = preop(x)
    return np.median(x, overwrite_input=True)

def nanmedian(x, axis=0, preop=None):
    """
    Compute the median along the given axis ignoring nan values.

    Parameters
    ----------
    x : array_like
        Input array.
    axis : int or None, optional
        Axis along which the median is computed. Default is 0.
        If None, compute over the whole array `x`.
    preop : function
        function to apply on 1d slice after removing the NaNs and before
        computing median

    Returns
    -------
    m : float
        The median of `x` along `axis`.

    See Also
    --------
    nanstd, nanmean, numpy.nanmedian

    Examples
    --------
    >>> from scipy import stats
    >>> a = np.array([0, 3, 1, 5, 5, np.nan])
    >>> stats.nanmedian(a)
    array(3.0)

    >>> b = np.array([0, 3, 1, 5, 5, np.nan, 5])
    >>> stats.nanmedian(b)
    array(4.0)

    Example with axis:

    >>> c = np.arange(30.).reshape(5,6)
    >>> idx = np.array([False, False, False, True, False] * 6).reshape(5,6)
    >>> c[idx] = np.nan
    >>> c
    array([[  0.,   1.,   2.,  nan,   4.,   5.],
           [  6.,   7.,  nan,   9.,  10.,  11.],
           [ 12.,  nan,  14.,  15.,  16.,  17.],
           [ nan,  19.,  20.,  21.,  22.,  nan],
           [ 24.,  25.,  26.,  27.,  nan,  29.]])
    >>> stats.nanmedian(c, axis=1)
    array([  2. ,   9. ,  15. ,  20.5,  26. ])

    """
    x = np.asarray(x)
    if axis is None:
        x = x.ravel()
        axis = 0
    if x.ndim == 0:
        return float(x.item())
    if preop is None and hasattr(np, 'nanmedian'):
        return np.nanmedian(x, axis)
    x = np.apply_along_axis(_nanmedian, axis, x, preop)
    if x.ndim == 0:
        x = float(x.item())
    return x

def avgOverCube(pixels, useAbsoluteValue=False, threshold=None, median=False, mask='', usermask=''):
    """
    Computes the average spectrum across a multi-dimensional
    array read from an image cube, ignoring any NaN values.
    Inputs:
    pixels: a 3D array (with degenerate Stokes axis dropped)
    threshold: value in Jy
    median: if True, compute the median instead of the mean
    mask: use pixels inside this spatial mask (i.e. with value==1 or True) to compute 
          the average (the spectral mask is taken as the union of all channels)
          This is the mask located inside the image cube specified in findContinuum(img='')
    usermask: this array results from the mask specified in findContinuum(mask='') parameter
    If threshold is specified, then it only includes pixels
    with an intensity above that value.
    Returns:
    * average spectrum
    * percentage of pixels not masked
    Note: This function assumes that the spectral axis is the final axis.
        If it is not, there is no single setting of the axis parameter that 
        can make it work.
    """
    if (useAbsoluteValue):
        pixels = np.abs(pixels)
    if (len(mask) > 0):
        npixels = np.prod(np.shape(pixels))
        before = np.count_nonzero(np.isnan(pixels))
        pixels[np.where(mask==False)] = np.nan
        after = np.count_nonzero(np.isnan(pixels))
        maskfalse = after-before
        print "Ignoring %d/%d pixels (%.2f%%) where mask is False" % (maskfalse, npixels, 100.*maskfalse/npixels)
    if (len(usermask) > 0):
        npixels = np.prod(np.shape(pixels))
        before = np.count_nonzero(np.isnan(pixels))
        pixels[np.where(usermask==0)] = np.nan
        after = np.count_nonzero(np.isnan(pixels))
        maskfalse = after-before
        print "Ignoring %d/%d pixels (%.2f%%) where usermask is 0" % (maskfalse, npixels, 100.*maskfalse/npixels)
    if (len(mask) > 0 or len(usermask) > 0):
        nonnan = np.prod(np.shape(pixels)) - np.count_nonzero(np.isnan(pixels))
        percentageUsed = 100.*nonnan/npixels
        print "Using %d/%d pixels (%.2f%%)" % (nonnan, npixels, percentageUsed)
    else:
        percentageUsed = 100
    nchan = np.shape(pixels)[-1]
    for i in range(nchan):
        if (len(np.shape(pixels)) == 4):
            channel = pixels[:,:,0,i].flatten()
        else:
            channel = pixels[:,:,i].flatten()
        validChan = len(np.where(channel >= threshold)[0])
        if (validChan < 1):
            casalogPost("ch%4d: none of the %d pixels meet the threshold in this channel" % (i,len(channel)))
    for i in range(len(np.shape(pixels))-1):
        if (median):
            pixels = nanmedian(pixels, axis=0)
        else:
            if (threshold is not None):
                idx = np.where(pixels < threshold)
                if len(idx[0]) > 0:
                    pixels[idx] = np.nan
            pixels = nanmean(pixels, axis=0)
    return(pixels, percentageUsed)

def findSpectralAxis(img):
    """
    Finds the spectral axis number of an image tool instance, or
    an image.
    """
    if (type(img) == str):
        myia = createCasaTool(iatool)
        myia.open(img)
    else:
        myia = img
    mycs = myia.coordsys()
    try:
        iax = mycs.findaxisbyname("spectral")
    except:
        print "ERROR: can't find spectral axis.  Assuming it is 3."
        iax = 3
    mycs.done()
    return iax

def submask(mask, region):
    """
    Takes a spectral mask array, and picks out a subcube defined by a blc,trc-defined region
    """
    mask = mask[region['blc'][0]:region['trc'][0]+1, region['blc'][1]:region['trc'][1]+1]
    return mask

def propagateMaskToAllChannels(mask, axis=3):
    """
    Takes a spectral mask array, and propagates every masked spatial pixel to 
    all spectral channels of the array.
    Returns: a 2D mask (of the same spatial dimension as the input, but only 1 channel)
    """
    casalogPost("Propagating image mask to all channels")
    startTime = timeUtilities.time()
    newmask = np.sum(mask,axis=axis)
    newmask[np.where(newmask>0)] = 1
    casalogPost("  elapsed time = %.1f sec" % (timeUtilities.time()-startTime))
    return(newmask)

def meanSpectrum(img='g35.03_KDnh3_11.hline.self.image', nBaselineChannels=16,
                 sigmaCube=3, verbose=False, nanBufferChannels=2, useAbsoluteValue=False,
                 baselineMode='edge', percentile=20, continuumThreshold=None,
                 meanSpectrumFile='', centralArcsec=-1, imageInfo=[], chanInfo=[], mask='',
                 meanSpectrumMethod='peakOverRms', peakFilterFWHM=15, iteration=0, applyMaskToMask=False):
    """
    Computes the average spectrum across a CASA image cube, via the specified method.
    Inputs:
    nBaselineChannels: number of channels to use as the baseline in 
                       the 'meanAboveThreshold' methods.
    baselineMode: how to select the channels to use as the baseline:
              'edge': use an equal number of channels on each edge of the spw
               'min': use the percentile channels with the lowest absolute intensity
    sigmaCube: multiply this value by the rms to get the threshold above which a pixel
               is included in the mean spectrum
    nanBufferChannels: when removing or replacing NaNs, do this many extra channels
                       beyond their actual extent
    useAbsoluteValue: passed to avgOverCube
    percentile: used with baselineMode='min'
    continuumThreshold: if specified, only use pixels above this intensity level
    meanSpectrumFile: name of ASCII file to produce to speed up future runs 
    centralArcsec: default=-1 means whole field, or a floating point value in arcsec
    mask: a mask image (e.g. from clean); restrict calculations to pixels with mask=1
    meanSpectrumMethod: 'peakOverMad', 'peakOverRms', 'meanAboveThreshold', 
       'meanAboveThresholdOverRms', 'meanAboveThresholdOverMad', 
    * 'meanAboveThreshold': uses a selection of baseline channels to compute the 
        rms to be used as a threshold value (similar to constructing a moment map).
    * 'meanAboveThresholdOverRms/Mad': scale spectrum by RMS or MAD        
    * 'peakOverRms/Mad' computes the ratio of the peak in a spatially-smoothed 
        version of cube to the RMS or MAD.  Smoothing is set by peakFilterFWHM.
    peakFilterFWHM: value used by 'peakOverRms' and 'peakOverMad' to presmooth 
        each plane with a Gaussian kernel of this width (in pixels)
        Set to 1 to not do any smoothing.
    Returns 8 items:
       * avgspectrum (vector)
       * avgspectrumAboveThresholdNansRemoved (vector)
       * avgspectrumAboveThresholdNansReplaced (vector)
       * threshold (scalar) 
       * edgesUsed: 0=lower, 1=upper, 2=both
       * nchan (scalar)
       * nanmin (the value used to replace NaNs)
       * percentagePixelsNotMasked
    """
    if (not os.path.exists(img)):
        casalogPost("Could not find image = %s" % (img))
        return
    myia = createCasaTool(iatool)
    usermaskdata = ''
    if (len(mask) > 0):
        # This is the user mask (not the minpb mask inside the cube).
        myia.open(mask)
        usermaskdata = myia.getregion()
        if (verbose): print "shape(usermask) = ", np.array(np.shape(usermaskdata))
        if applyMaskToMask:
            usermaskmask = myia.getregion(getmask=True)
            idx = np.where(usermaskmask==False)
            casalogPost('applyMaskToMask has zeroed out %d pixels.' % (len(idx[0])))
            usermaskdata[idx] = 0
        maskAxis = findSpectralAxis(myia)
        if (np.shape(usermaskdata)[maskAxis] > 1):
            singlePlaneUserMask = False
        else:
            singlePlaneUserMask = True
            if (meanSpectrumMethod.find('meanAboveThreshold') >= 0):
                casalogPost("single plane user masks not supported by meanSpectrumMethod='meanAboveThreshold', try peakOverMad.")
                myia.close()
                return
        myia.close()
    myia.open(img)
    axis = findSpectralAxis(myia)
    if verbose: print "Found spectral axis = ", axis
    myrg = None
    if (centralArcsec < 0 or centralArcsec == 'auto'):
        centralArcsec = -1
        if (len(mask) > 0 or meanSpectrumMethod != 'peakOverMad'):
            pixels = myia.getregion()
            maskdata = myia.getregion(getmask=True)
            nchan = np.shape(maskdata)[axis]
        else:
            bmaj, bmin, bpa, cdelt1, cdelt2, naxis1, naxis2, freq = imageInfo
            blc = [0,0,0,0]
            trc = [naxis1-1,naxis2-1,0,0]
            nchan = chanInfo[0]
            myrg = createCasaTool(rgtool)
    else:
        myrg = createCasaTool(rgtool)
        bmaj, bmin, bpa, cdelt1, cdelt2, naxis1, naxis2, freq = imageInfo
        nchan = chanInfo[0]
        x0 = int(np.round(naxis1*0.5 - centralArcsec*0.5/np.abs(cdelt1)))
        x1 = int(np.round(naxis1*0.5 + centralArcsec*0.5/np.abs(cdelt1)))
        y0 = int(np.round(naxis2*0.5 - centralArcsec*0.5/cdelt2))
        y1 = int(np.round(naxis2*0.5 + centralArcsec*0.5/cdelt2))
        # avoid going off the edge of non-square images
        if (x0 < 0): x0 = 0
        if (y0 < 0): y0 = 0
        if (x0 >= naxis1): x0 = naxis1 - 1
        if (y0 >= naxis2): y0 = naxis2 - 1
        blc = [x0,y0,0,0]
        trc = [x1,y1,0,0]
        trc[axis] = nchan
        region = myrg.box(blc=blc, trc=trc)
        pixels = myia.getregion(region=region)
        casalogPost("Taking submask for central area of image: blc=%s, trc=%s" % (str(blc),str(trc)))
        maskdata = myia.getregion(region=region,getmask=True)
#        myrg.done()
        if (len(mask) > 0):
            usermaskdata = submask(usermaskdata, region)
        if verbose:
            print "shape of pixels = ", np.array(np.shape(pixels))
    if len(mask) > 0:
        if not (np.array(np.shape(pixels)) == np.array(np.shape(usermaskdata))).all():
            casalogPost("Mismatch in shape between image (%s) and mask (%s)" % (np.shape(pixels),np.shape(usermaskdata)))
            if myrg is not None: myrg.done()
            return
    if (meanSpectrumMethod.find('OverRms') > 0 or meanSpectrumMethod.find('OverMad') > 0):
        # compute myrms, ignoring masked values and usermasked values
        if (meanSpectrumMethod.find('OverMad') < 0):
            casalogPost("Computing std on each plane")
        else:
            casalogPost("Computing mad on each plane")
        myvalue = []
#        for a in range(np.shape(pixels)[axis]):
        for a in range(nchan):
            if ((a+1)%100 == 0): 
                print "Done %d/%d" % (a+1, nchan)
#                    print "Done %d/%d" % (a+1, np.shape(pixels)[axis])
            # Extract this one channel
            if (axis == 2):
                if len(mask) > 0:
                    mypixels = pixels[:,:,a,0]
                    mymask = maskdata[:,:,a,0]
                    if (singlePlaneUserMask):
                        myusermask = usermaskdata[:,:,0,0]
                    else:
                        myusermask = usermaskdata[:,:,a,0]
                else:
                    blc[axis] = a
                    trc[axis] = a
                    myregion = myrg.box(blc=blc,trc=trc)
                    mypixels = myia.getregion(region=myregion)
                    mymask = myia.getregion(region=myregion,getmask=True)
            elif (axis == 3):
                if (len(mask) > 0):
                    mypixels = pixels[:,:,0,a]
                    mymask = maskdata[:,:,0,a]
                    if (singlePlaneUserMask):
                        myusermask = usermaskdata[:,:,0,0]
                    else:
                        myusermask = usermaskdata[:,:,0,a]
                else:
                    blc[axis] = a
                    trc[axis] = a
                    myregion = myrg.box(blc=blc,trc=trc)
                    mypixels = myia.getregion(region=myregion)
                    mymask = myia.getregion(region=myregion,getmask=True)
                    
            if (len(mask) > 0):
                # user mask is typically a clean mask, so we want to use the region outside the
                # clean mask for computing the MAD, but also avoiding the masked edges of the image,
                # which are generally masked to False
                pixelsForStd = mypixels[np.where((myusermask<1) * (mymask==True))]
            else: 
                # avoid the masked (typically outer) edges of the image using the built-in mask
                pixelsForStd = mypixels[np.where(mymask==True)]
            if (meanSpectrumMethod.find('OverMad') < 0):
                myvalue.append(np.std(pixelsForStd))
            else:
                myvalue.append(MAD(pixelsForStd))
#            print "channel %4d: Using %d of %d pixels for MAD/std" % (a,len(pixelsForStd),np.prod(np.shape(mypixels)))
        if (meanSpectrumMethod.find('OverMad') < 0):
            myrms = np.array(myvalue)
        else:
            mymad = np.array(myvalue)
        print "Finished"
    percentagePixelsNotMasked = 100
    if (meanSpectrumMethod.find('peakOver') == 0):
        # compute mymax (an array of channel maxima), then divide by either myrms or mymad array
        gaussianSigma = peakFilterFWHM/2.355
        myvalue = []
        casalogPost("B) Current memory usage: %.3f GB, resident: %.3f GB" % (memoryUsage(), residentMemoryUsage()))
        casalogPost("Smoothing and computing peak on each plane.")
        if (len(mask) > 0):
            pixels[np.where(usermaskdata==0)] = np.nan
        for a in range(nchan):
            if ((a+1)%100 == 0): 
                    print "Done %d/%d" % (a+1, nchan)
            if (axis == 2):
                if len(mask) > 0:
                    mypixels = pixels[:,:,a,0]
                else:
                    blc[axis] = a
                    trc[axis] = a
                    myregion = myrg.box(blc=blc,trc=trc)
                    mypixels = myia.getregion(region=myregion)
            elif (axis == 3):
                if len(mask) > 0:
                    mypixels = pixels[:,:,0,a]
                else:
                    blc[axis] = a
                    trc[axis] = a
                    myregion = myrg.box(blc=blc,trc=trc)
                    mypixels = myia.getregion(region=myregion)
            if (gaussianSigma > 1.1/2.355):
                if (len(mask) > 0):
                    # taken from stackoverflow.com/questions/18697532/gaussian-filtering-a-image-with-nan-in-python
                    V = mypixels.copy()
                    V[mypixels!=mypixels] = 0
                    VV = gaussian_filter(V,sigma=gaussianSigma)
                    W = mypixels.copy()+1
                    W[mypixels!=mypixels] = 0
                    WW = gaussian_filter(W,sigma=gaussianSigma)
                    mypixels = VV/WW
                    myvalue.append(np.nanmax(mypixels))
                else:
                    myvalue.append(np.nanmax(gaussian_filter(mypixels,sigma=gaussianSigma)))
            else:
                myvalue.append(np.nanmax(mypixels))
        print "finished"
        mymax = np.array(myvalue)
        if (meanSpectrumMethod == 'peakOverRms'):
            avgspectrum = mymax/myrms
        elif (meanSpectrumMethod == 'peakOverMad'):
            avgspectrum = mymax/mymad
        nansRemoved = removeNaNs(avgspectrum, verbose=True)
        threshold = 0
        edgesUsed = 0
        nansReplaced,nanmin = removeNaNs(avgspectrum, replaceWithMin=True, 
                                         nanBufferChannels=nanBufferChannels, verbose=True)
    elif (meanSpectrumMethod.find('meanAboveThreshold') == 0):
        if (continuumThreshold is not None):
            belowThreshold = np.where(pixels < continuumThreshold)
            if verbose:
                print "shape of belowThreshold = ", np.shape(belowThreshold)
            pixels[belowThreshold] = 0.0
        if (len(mask) > 0):
            pixelsWithinUserMask = len(np.where(usermaskdata<1)[0])
            pixelsWithinCubeMask = len(np.where(maskdata==1)[0])
            pixelsForMAD = pixels[np.where((maskdata==1) * (usermaskdata<1))]
            npixels = np.prod(np.shape(pixels))
            percent = 100.*len(pixelsForMAD) / npixels
            percent2 = 100.*pixelsWithinUserMask/npixels
            percent3 = 100.*pixelsWithinCubeMask/npixels
            casalogPost("Using %d of %d pixels (%.2f%%) for MAD:  %d (%.2f%%) outside user mask, %d (%.2f%%) satisfy cube mask, i.e. minpb masking" % (len(pixelsForMAD),npixels, percent, pixelsWithinUserMask, percent2, pixelsWithinCubeMask, percent3))
        else:
            pixelsForMAD = pixels[np.where(maskdata==1)]  # ignore the outer mask edges of the cube
            casalogPost("Using %d of %d pixels for MAD" % (len(pixelsForMAD),np.prod(np.shape(pixels))))
#            pixelsForMAD = pixels  # previous method
        madTime = timeUtilities.time()
        std = MAD(pixelsForMAD, axis=None)
        endMadTime = timeUtilities.time()
        casalogPost("%.1f sec elapsed in computing MAD within meanSpectrum()" % (endMadTime-madTime))
        if verbose: print "MAD of cube = ", std
        naxes = len(np.shape(pixels))
        nchan = np.shape(pixels)[axis]

        if (baselineMode == 'edge'):
            # Method #1: Use the two edges of the spw to find the line-free rms of the spectrum
            nEdgeChannels = nBaselineChannels/2
            # lower edge
            blc = np.zeros(naxes)
            trc = [i-1 for i in list(np.shape(pixels))]
            trc[axis] = nEdgeChannels
            myrg = createCasaTool(rgtool)
            region = myrg.box(blc=blc, trc=trc)
            lowerEdgePixels = myia.getregion(region=region)
            # drop all floating point zeros (which will drop pixels outside the mosaic image mask)
            lowerEdgePixels = lowerEdgePixels[np.where(lowerEdgePixels!=0.0)]
            stdLowerEdge = MAD(lowerEdgePixels)
            medianLowerEdge = nanmedian(lowerEdgePixels)
            if verbose: print "MAD of %d channels on lower edge = %f" % (nBaselineChannels, stdLowerEdge)

            # upper edge
            blc = np.zeros(naxes)
            trc = [i-1 for i in list(np.shape(pixels))]
            blc[axis] = trc[axis] - nEdgeChannels
            region = myrg.box(blc=blc, trc=trc)
            upperEdgePixels = myia.getregion(region=region)
#            myrg.done()
            # drop all floating point zeros
            upperEdgePixels = upperEdgePixels[np.where(upperEdgePixels!=0.0)]
            stdUpperEdge = MAD(upperEdgePixels)
            medianUpperEdge = nanmedian(upperEdgePixels)
            casalogPost("meanSpectrum(): edge medians: lower=%.10f, upper=%.10f" % (medianLowerEdge, medianUpperEdge))

            if verbose: 
                print "MAD of %d channels on upper edge = %f" % (nEdgeChannels, stdUpperEdge)
            if (stdLowerEdge <= 0.0):
                edgesUsed = 1
                stdEdge = stdUpperEdge
                medianEdge = medianUpperEdge
            elif (stdUpperEdge <= 0.0):
                edgesUsed = 0
                stdEdge = stdLowerEdge
                medianEdge = medianLowerEdge
            else:
                edgesUsed = 2
                stdEdge = np.mean([stdLowerEdge,stdUpperEdge])
                medianEdge = np.mean([medianLowerEdge,medianUpperEdge])
        
        if (baselineMode != 'edge'):
            # Method #2: pick the N channels with the lowest absolute values (to avoid
            #          confusion from absorption lines and negative bowls of missing flux)
            npixFraction = nBaselineChannels*1.0/nchan
            if (centralArcsec < 0):
                allPixels = myia.getregion()
            else:
                allPixels = pixels
            myia.close()
            # Convert all NaNs to zero
            allPixels[np.isnan(allPixels)] = 0
            # Drop all floating point zeros and internally-masked pixels from calculation
            if (mask == ''):
                allPixels = allPixels[np.where((allPixels != 0) * (maskdata==True))]
            else:
                # avoid identical zeros and clean mask when looking for lowest pixels
                allPixels = allPixels[np.where((allPixels != 0) * (maskdata==True) * (usermaskdata<1))]
            # Take absolute value
            absPixels = np.abs(allPixels)
            # Find the lowest pixel values
            percentileThreshold = scoreatpercentile(absPixels, percentile)
            idx = np.where(absPixels < percentileThreshold)
            # Take their statistics
            stdMin = MAD(allPixels[idx])
            medianMin = nanmedian(allPixels[idx])

        if (baselineMode == 'edge'):
            std = stdEdge
            median = medianEdge
            casalogPost("meanSpectrum(): edge mode:  median=%f  MAD=%f  threshold=%f (edgesUsed=%d)" % (medianEdge, stdEdge, medianEdge+stdEdge*sigmaCube, edgesUsed))
        else:
            std = stdMin
            median = medianMin
            edgesUsed = 0
            casalogPost("meanSpectrum(): min mode:  median=%f  MAD=%f  threshold=%f" % (medianMin, stdMin, medianMin+stdMin*sigmaCube))
        
        if (axis == 2 and naxes == 4):
            # drop the degenerate axis so that avgOverCube will work with nanmean(axis=0)
            pixels = pixels[:,:,:,0]
        if (len(mask) > 0):
            maskdata = propagateMaskToAllChannels(maskdata, axis)
        else:
            maskdata = ''
        avgspectrum, percentagePixelsNotMasked = avgOverCube(pixels, useAbsoluteValue, mask=maskdata, usermask=usermaskdata)
        if meanSpectrumMethod.find('OverRms') > 0:
            avgspectrum /= myrms
        elif meanSpectrumMethod.find('OverMad') > 0:
            avgspectrum /= mymad
        threshold = median + sigmaCube*std
        casalogPost("Using threshold above which to compute mean spectrum = %f" % (threshold), verbose)
        pixels[np.where(pixels < threshold)] = 0.0
        casalogPost("Running avgOverCube")
        avgspectrumAboveThreshold, percentagePixelsNotMasked = avgOverCube(pixels, useAbsoluteValue, threshold, mask=maskdata, usermask=usermaskdata)
        if meanSpectrumMethod.find('OverRms') > 0:
            avgspectrumAboveThreshold /= myrms
        elif meanSpectrumMethod.find('OverMad') > 0:
            avgspectrumAboveThreshold /= mymad
        if verbose: 
            print "Running removeNaNs (len(avgspectrumAboveThreshold)=%d)" % (len(avgspectrumAboveThreshold))
        nansRemoved = removeNaNs(avgspectrumAboveThreshold)
        nansReplaced,nanmin = removeNaNs(avgspectrumAboveThreshold, replaceWithMin=True, 
                                         nanBufferChannels=nanBufferChannels)
    nchan, firstFreq, lastFreq, channelWidth = chanInfo
    frequency = np.linspace(firstFreq, lastFreq, nchan)
    if verbose: 
        print "Running writeMeanSpectrum"
    writeMeanSpectrum(meanSpectrumFile, frequency, avgspectrum, nansReplaced, threshold,
                      edgesUsed, nchan, nanmin, centralArcsec, mask, iteration)
    if (myrg is not None): myrg.done()
    return(avgspectrum, nansRemoved, nansReplaced, threshold, 
           edgesUsed, nchan, nanmin, percentagePixelsNotMasked)

def removeNaNs(a, replaceWithMin=False, verbose=False, nanBufferChannels=0, 
               replaceWithZero=False):
    """
    Remove or replace the nan values from an array.
    replaceWithMin: if True, then replace NaNs with np.nanmin of array
                    if False, then simply remove the NaNs
    replaceWithZero: if True, then replace NaNs with np.nanmin of array
                    if False, then simply remove the NaNs
    nanBufferChannels: only active if replaceWithMin=True
    Returns:
    * new array
    * if replaceWithMin=True, then also return the value used to replace NaNs
    """
    a = np.array(a) 
    if (len(a) < 1): return(a)
    startLength = len(a)
    if (replaceWithMin or replaceWithZero):
        idx = np.isnan(a)
        print "Found %d nan channels: %s" % (len(np.where(idx==True)[0]), np.where(idx==True)[0])
        idx2 = np.isinf(a)
        print "Found %d inf channels" % (len(np.where(idx2==True)[0]))
        a_nanmin = np.nanmin(a)
        if (nanBufferChannels > 0):
            idxlist = splitListIntoHomogeneousLists(idx)
            idx = []
            for i,mylist in enumerate(idxlist):
                if (mylist[0]):
                    idx += mylist
                else:
                    newSubString = nanBufferChannels*[True] 
                    if (i < len(idxlist)-1):
                        newSubString += mylist[nanBufferChannels:-nanBufferChannels] + nanBufferChannels*[True]
                    else:
                        newSubString += mylist[-nanBufferChannels:]
                    # If the channel block was less than 2*nanBufferChannels wide, then only insert up to its width
                    idx += newSubString[:len(mylist)]
            idx = np.array(idx)
        if (verbose):
            print "Replaced %d NaNs" % (len(idx))
            print "Replaced %d infs" % (len(idx2))
        if (replaceWithMin):
            a[idx] = a_nanmin
            a[idx2] = a_nanmin
            return(a, a_nanmin)
        elif (replaceWithZero):
            a[idx] = 0
            a[idx2] = 0
            return(a, 0)
    else:
        a = a[np.where(np.isnan(a) == False)]
        if (verbose):
            print "Removed %d NaNs" % (startLength-len(a))
        startLength = len(a)
        a = a[np.where(np.isinf(a) == False)]
        if (verbose):
            print "Removed %d infs" % (startLength-len(a))
        return(a)

def create_casa_quantity(myqatool,value,unit):
    """
    A wrapper to handle the changing ways in which casa quantities are invoked.
    Todd Hunter
    """
    if (type(casac.Quantity) != type):  # casa 4.x
        myqa = myqatool.quantity(value, unit)
    else:  # casa 3.x
        myqa = casac.Quantity(value, unit)
    return(myqa)

def createCasaTool(mytool):
    """
    A wrapper to handle the changing ways in which casa tools are invoked.
    Todd Hunter
    """
    if (type(casac.Quantity) != type):  # casa 4.x
        myt = mytool()
    else:  # casa 3.x
        myt = mytool.create()
    return(myt)

def MAD(a, c=0.6745, axis=0):
    """
    Median Absolute Deviation along given axis of an array:
         median(abs(a - median(a))) / c
    c = 0.6745 is the constant to convert from MAD to std 
    """
    a = np.asarray(a, np.float64)
    m = nanmedian(a, axis=axis,
                  preop=lambda x:
                        np.fabs(np.subtract(x, np.median(x, axis=None), out=x), out=x))
    return m / c

def splitListIntoContiguousLists(mylist):
    """
    Called by copyweights. See also splitListIntoHomogenousLists.
    Converts [1,2,3,5,6,7] into [[1,2,3],[5,6,7]], etc.
    -Todd Hunter
    """
    mylists = []
    if (len(mylist) < 1):
        return(mylists)
    newlist = [mylist[0]]
    for i in range(1,len(mylist)):
        if (mylist[i-1] != mylist[i]-1):
            mylists.append(newlist)
            newlist = [mylist[i]]
        else:
            newlist.append(mylist[i])
    mylists.append(newlist)
    return(mylists)

def splitListIntoContiguousListsAndRejectZeroStd(channels, values, nanmin=None, verbose=False):
    """
    Takes a list of numerical values, splits into contiguous lists and 
    removes those that have zero standard deviation in their associated values.
    Note that values *must* hold the values of the whole array, while channels 
    can be a subset.
    If nanmin is specified, then reject lists that contain more than 3 
    appearances of this value.
    """
    if verbose:
        print "splitListIntoContiguousListsAndRejectZeroStd:  len(values)=%d, len(channels)=%d" % (len(values), len(channels))
    values = np.array(values)
    mylists = splitListIntoContiguousLists(channels)
    channels = []
    for i,mylist in enumerate(mylists):
        mystd = np.std(values[mylist])
        if (mystd > 1e-17):  # avoid blocks of identically-zero values
            if (nanmin is not None):
                minvalues = len(np.where(values[i] == nanmin)[0])
                if (float(minvalues)/len(mylist) > 0.1 and minvalues > 3):
                    print "Rejecting list %d with multiple min values (%d)" % (i,minvalues)
                    continue
            channels += mylist
    return(np.array(channels))

def splitListIntoHomogeneousLists(mylist):
    """
    Converts [1,1,1,2,2,3] into [[1,1,1],[2,2],[3]], etc.
    -Todd Hunter
    """
    mylists = []
    newlist = [mylist[0]]
    for i in range(1,len(mylist)):
        if (mylist[i-1] != mylist[i]):
            mylists.append(newlist)
            newlist = [mylist[i]]
        else:
            newlist.append(mylist[i])
    mylists.append(newlist)
    return(mylists)
    
def convertChannelListIntoSelection(channels, trim=0, separator=';'):
    """
    Converts a list of channels into casa selection string syntax.
    channels: a list of channels
    trim: the number of channels to trim off of each edge of a block of channels
    """
    selection = ''
    firstList = True
    if (len(channels) > 0):
        mylists = splitListIntoContiguousLists(channels)
        for mylist in mylists:
            if (mylist[0]+trim <= mylist[-1]-trim):
                if (not firstList):
                    selection += separator
                selection += '%d~%d' % (mylist[0]+trim, mylist[-1]-trim)
                firstList = False
    return(selection)

def getFreqsForImage(img, header='', spectralaxis=-1, unit='GHz'):
    """
    Returns an array of frequencies of an image cube in the specified unit.
    Not currently used.
    """
    if (header == ''):
        header = imhead(img,mode='list')
    myia = createCasaTool(iatool)
    myia.open(img)
    if (spectralaxis < 0):
        spectralaxis = findSpectralAxis(myia)
    startchan = [None,None,None,None]
    stopchan = [None,None,None,None]
    startchan[spectralaxis] = 0
    nchan = header['shape'][spectralaxis]
    stopchan[spectralaxis] = nchan
    myqa = createCasaTool(qatool)
    m0 = myia.coordmeasures(startchan)['measure']['spectral']['frequency']['m0']
    m1 = myia.coordmeasures(stopchan)['measure']['spectral']['frequency']['m0']
    f0 = myqa.convert('%.15e %s' % (m0['value'], m0['unit']), unit)
    f1 = myqa.convert('%.15e %s' % (m1['value'], m1['unit']), unit)
    freqs = np.linspace(f0['value'],f1['value'],nchan)
    myia.close()
    myqa.done()
    return freqs

def CalcAtmTransmissionForImage(img, header='', chanInfo='', airmass=1.5,pwv=-1,                                
                                spectralaxis=-1, 
                                value='transmission', P=-1, H=-1, 
                                T=-1, altitude=-1):
    """
    supported telescopes are VLA and ALMA (needed for default weather and PWV)
    value: 'transmission' or 'tsky'
    chanInfo: a list containing nchan, firstFreqHz, lastFreqHz, channelWidthHz
    pwv: in mm
    P: in mbar
    H: in percent
    T: in Kelvin
    Returns:
    2 arrays: frequencies (in GHz) and values (Kelvin, or transmission: 0..1)
    """
    if (header == ''):
        print "imhead", # the comma prevents the newline so that ...10...20 will be on same line
        header = imhead(img,mode='list')
    if (type(header) != dict):
        # Input was a spectrum rather than an image
        if (chanInfo[1] < 60e9):
            telescopeName = 'ALMA'
        else:
            telescopeName = 'VLA'
    else:
        telescopeName = header['telescope']
        # this will not match up with the plot, which uses numberOfChannelsInCube
#        freqs = getFreqsForImage(img, header, spectralaxis)
    freqs = np.linspace(chanInfo[1]*1e-9,chanInfo[2]*1e-9,chanInfo[0])
#    print "freqs: %f-%f" % (freqs[0], freqs[-1])
    numchan = len(freqs)
    lsrkwidth = (chanInfo[2] - chanInfo[1])/(numchan-1)
    result = cubeLSRKToTopo(img, nchan=numchan, f0=chanInfo[1], f1=chanInfo[2], chanwidth=lsrkwidth)
    if (result is None):
        topofreqs = freqs
    else:
        topoWidth = (result[1]-result[0])/(numchan-1)
        topofreqs = np.linspace(result[0], result[1], chanInfo[0]) * 1e-9
        casalogPost("Converted LSRK range (%f-%f) to TOPO (%f-%f) over %d channels" % (chanInfo[1]*1e-9, chanInfo[2]*1e-9,topofreqs[0],topofreqs[-1],numchan))
    P0 = 1000.0 # mbar
    H0 = 20.0   # percent
    T0 = 273.0  # Kelvin
    if (telescopeName.find('ALMA') >= 0 or telescopeName.find('ACA') >= 0):
        pwv0 = 1.0   
        P0 = 563.0
        H0 = 20.0
        T0 = 273.0
        altitude0 = 5059
    elif (telescopeName.find('VLA') >= 0):
        P0 = 786.0
        pwv0 = 5.0  
        altitude0 = 2124
    else:
        pwv0 = 10.0  
        altitude0 = 0
    if (pwv < 0):
        pwv = pwv0
    if (T < 0):
        T = T0
    if (H < 0):
        H = H0
    if (P < 0):
        P = P0
    if (altitude < 0):
        altitude = altitude0
    tropical = 1
    midLatitudeSummer = 2
    midLatitudeWinter = 3
#    print "image bandwidth = %f GHz" % (np.max(freqs)-np.min(freqs))
    reffreq = np.mean(topofreqs)
    numchanModel = numchan*1
    chansepModel = (topofreqs[-1]-topofreqs[0])/(numchanModel-1)
#    print "regridded bandwidth=%f GHz, chansep=%f, reffreq=%f" % (np.max(topofreqs)-np.min(topofreqs), chansepModel, reffreq)
    nbands = 1
    myqa = createCasaTool(qatool)
    fCenter = create_casa_quantity(myqa, reffreq, 'GHz')
    fResolution = create_casa_quantity(myqa, chansepModel, 'GHz')
    fWidth = create_casa_quantity(myqa, numchanModel*chansepModel, 'GHz')
    myat = casac.atmosphere()
    myat.initAtmProfile(humidity=H, temperature=create_casa_quantity(myqa,T,"K"),
                        altitude=create_casa_quantity(myqa,altitude,"m"),
                        pressure=create_casa_quantity(myqa,P,'mbar'),atmType=midLatitudeWinter)
    myat.initSpectralWindow(nbands, fCenter, fWidth, fResolution)
    myat.setUserWH2O(create_casa_quantity(myqa, pwv, 'mm'))
#    myat.setAirMass()  # This does not affect the opacity, but it does effect TebbSky, so do it manually.
    myqa.done()

    dry = np.array(myat.getDryOpacitySpec(0)[1])
    wet = np.array(myat.getWetOpacitySpec(0)[1]['value'])
    TebbSky = myat.getTebbSkySpec(spwid=0)[1]['value']
    # readback the values to be sure they got set
    
    rf = myat.getRefFreq()['value']
    cs = myat.getChanSep()['value']
    if (myat.getRefFreq()['unit'] != 'GHz'):
        casalogPost("There is a unit mismatch for refFreq in the atm code.")
    if (myat.getChanSep()['unit'] != 'MHz'):
        casalogPost("There is a unit mismatch for chanSep in the atm code.")
    numchanModel = myat.getNumChan()
    freq0 = myat.getChanFreq(0)['value']
    freq1 = myat.getChanFreq(numchanModel-1)['value']
#    print "atm returned bandwidth = %f GHz = %f to %f " % (freq1-freq0, freq0, freq1)
    newfreqs = np.linspace(freqs[0], freqs[-1], numchanModel)  # fix for SCOPS-4815
#    print "freqs: %f-%f  newfreqs: %f-%f" % (freqs[0], freqs[-1], newfreqs[0], newfreqs[-1])
    transmission = np.exp(-airmass*(wet+dry))
    TebbSky *= (1-np.exp(-airmass*(wet+dry)))/(1-np.exp(-wet-dry))
    if value=='transmission':
        values = transmission
    else:
        values = TebbSky
    del myat
    return(newfreqs, values)

def mjdToUT(mjd=None, use_metool=True, prec=6):
    """
    Converts an MJD value to a UT date and time string
    such as '2012-03-14 00:00:00 UT'
    use_metool: whether or not to use the CASA measures tool if running from CASA.
         This parameter is simply for testing the non-casa calculation.
    -Todd Hunter
    """
    if mjd is None:
        mjdsec = getCurrentMJDSec()
    else:
        mjdsec = mjd*86400
    utstring = mjdSecondsToMJDandUT(mjdsec, use_metool, prec=prec)[1]
    return(utstring)
        
def call_qa_time(arg, form='', prec=0, showform=False):
    """
    This is a wrapper for qa.time(), which in casa 4.0.0 returns a list 
    of strings instead of just a scalar string.  
    - Todd Hunter
    """
    if (type(arg) == dict):
        if (type(arg['value']) == list or 
            type(arg['value']) == np.ndarray):
            if (len(arg['value']) > 1):
                print "WARNING: call_qa_time() received a dictionary containing a list of length=%d rather than a scalar. Using first value." % (len(arg['value']))
            arg['value'] = arg['value'][0]
    result = qa.time(arg, form=form, prec=prec, showform=showform)
    if (type(result) == list or type(result) == np.ndarray):
        return(result[0])
    else:
        return(result)

def mjdsecToUT(mjdsec=None, prec=6, measuresToolFormat=False):
    """
    Converts an MJD seconds value to a UT date and time string
    such as '2012-03-14 00:00:00 UT'.  For a string with only the
    date, use mjdsecToDate().
    -Todd Hunter
    """
    if mjdsec is None: mjdsec = getCurrentMJDSec()
    if (type(mjdsec) == list or type(mjdsec) == np.ndarray):
        retval = [mjdSecondsToMJDandUT(m, prec=prec)[1] for m in mjdsec]
        if measuresToolFormat:
            retval = [mjdSecondsToMJDandUT(m, prec=prec)[1].replace(' UT','').replace('-','/').replace(' ','/') for m in mjdsec]
    else:
        retval = mjdSecondsToMJDandUT(mjdsec, prec=prec)[1]
        if measuresToolFormat:
            retval = retval.replace(' UT','').replace('-','/').replace(' ','/')
    return(retval)
        
def mjdSecondsToMJDandUT(mjdsec, debug=False, prec=6, delimiter='-'):
    """
    Converts a value of MJD seconds into MJD, and into a UT date/time string.
    prec: 6 means HH:MM:SS,  7 means HH:MM:SS.S
    example: (56000.0, '2012-03-14 00:00:00 UT')
    Caveat: only works for a scalar input value
    Todd Hunter
    """
    myme = createCasaTool(metool)
    today = myme.epoch('utc','today')
    mjd = np.array(mjdsec) / 86400.
    today['m0']['value'] =  mjd
    hhmmss = call_qa_time(today['m0'], prec=prec)
    date = qa.splitdate(today['m0'])
    myme.done()
    utstring = "%s%s%02d%s%02d %s UT" % (date['year'],delimiter,date['month'],delimiter,
                                             date['monthday'],hhmmss)
    return(mjd, utstring)

def getCurrentMJDSec():
    """
    Returns the current MJD in seconds.
    Todd Hunter
    """
    mjdsec = getMJD() * 86400
    return(mjdsec)

def getMJD():
    """
    Returns the current MJD.  See also getCurrentMJDSec().
    -Todd
    """
    myme = createCasaTool(metool)
    mjd = myme.epoch('utc','today')['m0']['value']
    myme.done()
    return(mjd)
    
def getObservationMJDSecRange(vis):
    """
    Returns 2 floating point values in MJD seconds.
    """
    return([getObservationStart(vis),getObservationStop(vis)])

def getObservationStart(vis, obsid=-1, verbose=False):
    """
    Reads the start time of the observation from the OBSERVATION table and reports it in MJD seconds.
    obsid: if -1, return the start time of the earliest obsID
    -Todd Hunter
    """
    if (os.path.exists(vis) == False):
        print "vis does not exist = %s" % (vis)
        return
    if (os.path.exists(vis+'/table.dat') == False):
        print "No table.dat.  This does not appear to be an ms."
        print "Use au.getObservationStartDateFromASDM()."
        return
    mytb = createCasaTool(tbtool)
    try:
        mytb.open(vis+'/OBSERVATION')
    except:
        print "ERROR: failed to open OBSERVATION table on file "+vis
        return(3)
    time_range = mytb.getcol('TIME_RANGE')
    mytb.close()
    if verbose:  print "time_range: ", str(time_range)
    # the first index is whether it is starttime(0) or stoptime(1) 
    time_range = time_range[0]
    if verbose:  print "time_range[0]: ", str(time_range)
    if (obsid >= len(time_range)):
        print "Invalid obsid"
        return
    if obsid >= 0:
        time_range = time_range[obsid]
    elif (type(time_range) == np.ndarray):
        time_range = np.min(time_range)
    return(time_range)

def getObservationStop(vis, obsid=-1, verbose=False):
    """
    Read the stop time of the observation and report it in MJD seconds.
    obsid: if -1, return the stop time of the latest obsID
    -Todd Hunter
    """
    if (os.path.exists(vis) == False):
        print "vis does not exist = %s" % (vis)
        return(2)
    if (os.path.exists(vis+'/table.dat') == False):
        print "No table.dat.  This does not appear to be an ms."
        return
    mytb = createCasaTool(tbtool)
    try:
        mytb.open(vis+'/OBSERVATION')
    except:
        print "ERROR: failed to open OBSERVATION table on file "+vis
        return(3)
    time_range = mytb.getcol('TIME_RANGE')
    mytb.close()
    if verbose:  print time_range
    # the first index is whether it is starttime(0) or stoptime(1) 
    time_range = time_range[1]
    if (obsid >= len(time_range)):
        print "Invalid obsid"
        return
    if obsid >= 0:
        time_range = time_range[obsid]
    elif (type(time_range) == np.ndarray):
        time_range = np.max(time_range)
    return(time_range)

if False:
  def getObservationStartDate(vis, obsid=0, delimiter='-', measuresToolFormat=False):
    """
    Read the start time of the observation and reports the date.
    Returns: '2013-01-31 07:36:01 UT'
    measuresToolFormat: if True, then return '2013/01/31/07:36:01'
    -Todd Hunter
    """
    mjdsec = getObservationStart(vis, obsid)
    if (mjdsec is None):
        return
    obsdateString = mjdToUT(mjdsec/86400.)
    if (delimiter != '-'):
        obsdateString = obsdateString.replace('-', delimiter)
    if measuresToolFormat:
        return(obsdateString.replace(' UT','').replace(delimiter,'/').replace(' ','/'))
    else:
        return(obsdateString)

def cubeLSRKToTopo(img, freqrange='', prec=4, verbose=False, 
                   nchan=None, f0=None, f1=None, chanwidth=None,
                   vis=''):
    """
    Reads the date of observation, central RA and Dec,
    and observatory from an image cube header and then calls lsrkToTopo to
    return the specified frequency range in TOPO.
    freqrange: desired range of frequencies (empty string or list = whole cube)
          floating point list of two frequencies, or a delimited string
          (delimiter = ',', '~' or space)
    prec: in fractions of Hz (only used to display the value when verbose=True)
    vis: read date of observation from the specified measurement set
    -Todd Hunter
    """
    header = imheadlist(img, omitBeam=True)
    if ('reffreqtype' in header.keys()):
        if (header['reffreqtype'].upper() == 'TOPO'):
            print "This cube is purportedly already in TOPO."
            return
    if (nchan is None or f0 is None or f1 is None or chanwidth is None):
        nchan,f0,f1,chanwidth = numberOfChannelsInCube(img, returnFreqs=True, header=header, 
                                                       faster=True, returnChannelWidth=True)
    if len(freqrange) == 0:
        startFreq = f0 
        stopFreq = f1
    elif (type(freqrange) == str):
        if (freqrange.find(',') > 0):
            freqrange = [parseFrequencyArgument(i) for i in freqrange.split(',')]
        elif (freqrange.find('~') > 0):
            freqrange = [parseFrequencyArgument(i) for i in freqrange.split('~')]
        else:
            freqrange = [parseFrequencyArgument(i) for i in freqrange.split()]
        startFreq, stopFreq = freqrange
    else:
        startFreq, stopFreq = freqrange
    ra,dec = rad2radec(header['crval1'], header['crval2'],
                       delimiter=' ', verbose=False).split()
    equinox = header['equinox']
    observatory = header['telescope']
    if vis == '':
        datestring = header['date-obs']
    else:
#        datestring = getObservationStartDate(vis, measuresToolFormat=True)
        # get the datetime stamp of the middle of the measurement set
        datestring = mjdsecToUT(np.mean(getObservationMJDSecRange(vis)), measuresToolFormat=True)
    f0 = lsrkToTopo(startFreq, datestring, ra, dec, equinox, observatory, prec, verbose)
    f1 = lsrkToTopo(stopFreq, datestring, ra, dec, equinox, observatory, prec, verbose) 
    return(np.array([f0,f1]))

def rad2radec(ra=0,dec=0, prec=5, verbose=True, component=0,
              replaceDecDotsWithColons=True, hmsdms=False, delimiter=', ',
              prependEquinox=False, hmdm=False):
    """
    Convert a position in RA/Dec from radians to sexagesimal string which
    is comma-delimited, e.g. '20:10:49.01, +057:17:44.806'.
    The position can either be entered as scalars via the 'ra' and 'dec' 
    parameters, as a tuple via the 'ra' parameter, or as an array of shape (2,1)
    via the 'ra' parameter.
    replaceDecDotsWithColons: replace dots with colons as the Declination d/m/s delimiter
    hmsdms: produce output of format: '20h10m49.01s, +057d17m44.806s'
    hmdm: produce output of format: '20h10m49.01, +057d17m44.806' (for simobserve)
    delimiter: the character to use to delimit the RA and Dec strings output
    prependEquinox: if True, insert "J2000" before coordinates (i.e. for clean or simobserve)
    Todd Hunter
    """
    if (type(ra) == tuple or type(ra) == list or type(ra) == np.ndarray):
        if (len(ra) == 2):
            dec = ra[1] # must come first before ra is redefined
            ra = ra[0]
        else:
            ra = ra[0]
            dec = dec[0]
    if (np.shape(ra) == (2,1)):
        dec = ra[1][0]
        ra = ra[0][0]
    myqa = createCasaTool(qatool)
    myra = myqa.formxxx('%.12frad'%ra,format='hms',prec=prec+1)
    mydec = myqa.formxxx('%.12frad'%dec,format='dms',prec=prec-1)
    if replaceDecDotsWithColons:
        mydec = mydec.replace('.',':',2)
    if (len(mydec.split(':')[0]) > 3):
        mydec = mydec[0] + mydec[2:]
    mystring = '%s, %s' % (myra, mydec)
    myqa.done()
    if (hmsdms):
        mystring = convertColonDelimitersToHMSDMS(mystring)
        if (prependEquinox):
            mystring = "J2000 " + mystring
    elif (hmdm):
        mystring = convertColonDelimitersToHMSDMS(mystring, s=False)
        if (prependEquinox):
            mystring = "J2000 " + mystring
    if (delimiter != ', '):
        mystring = mystring.replace(', ', delimiter)
    if (verbose):
        print mystring
    return(mystring)

def convertColonDelimitersToHMSDMS(mystring, s=True, usePeriodsForDeclination=False):
    """
    Converts HH:MM:SS.SSS, +DD:MM:SS.SSS  to  HHhMMmSS.SSSs, +DDdMMmSS.SSSs
          or HH:MM:SS.SSS +DD:MM:SS.SSS   to  HHhMMmSS.SSSs +DDdMMmSS.SSSs
          or HH:MM:SS.SSS, +DD:MM:SS.SSS  to  HHhMMmSS.SSSs, +DD.MM.SS.SSS
          or HH:MM:SS.SSS +DD:MM:SS.SSS   to  HHhMMmSS.SSSs +DD.MM.SS.SSS
    s: whether or not to include the trailing 's' in both axes
    -Todd Hunter
    """
    colons = len(mystring.split(':'))
    if (colons < 5 and (mystring.strip().find(' ')>0 or mystring.find(',')>0)):
        print "Insufficient number of colons (%d) to proceed (need 4)" % (colons-1)
        return
    if (usePeriodsForDeclination):
        decdeg = '.'
        decmin = '.'
        decsec = ''
    else:
        decdeg = 'd'
        decmin = 'm'
        decsec = 's'
    if (s):
        outstring = mystring.strip(' ').replace(':','h',1).replace(':','m',1).replace(',','s,',1).replace(':',decdeg,1).replace(':',decmin,1) + decsec
        if (',' not in mystring):
            outstring = outstring.replace(' ', 's ',1)
    else:
        outstring = mystring.strip(' ').replace(':','h',1).replace(':','m',1).replace(':',decdeg,1).replace(':',decmin,1)
    return(outstring)
    
def lsrkToTopo(lsrkFrequency, datestring, ra, dec, equinox='J2000', observatory='ALMA', 
               prec=4, verbose=False):
    """
    Converts an LSRKfrequency and observing date/direction
    to the corresponding frequency in the TOPO frame.
    Inputs:
    lsrkFrequency: floating point value in Hz or GHz, or a string with units
    datestring:  "YYYY/MM/DD/HH:MM:SS" (format = image header keyword 'date-obs')
    ra: string "HH:MM:SS.SSSS"
    dec: string "DD.MM.SS.SSSS" or "DD:MM:SS.SSSS" (colons will be replaced with .)
    prec: only used to display the value when verbose=True
    Returns: the TOPO frequency in Hz
    -Todd Hunter
    """
    velocityLSRK = 0  # does not matter what it is, just needs to be same in both calls
    restFreqHz = lsrkToRest(lsrkFrequency, velocityLSRK, datestring, ra, dec, equinox,
                            observatory, prec, verbose)
    topoFrequencyHz = restToTopo(restFreqHz, velocityLSRK, datestring, ra, dec, equinox, 
                                observatory, verbose=verbose)
    return topoFrequencyHz

def lsrkToRest(lsrkFrequency, velocityLSRK, datestring, ra, dec, 
               equinox='J2000', observatory='ALMA', prec=4, verbose=True):
    """
    Converts an LSRK frequency, LSRK velocity, and observing date/direction
    to the corresponding frequency in the rest frame.
    Inputs:
    lsrkFrequency: floating point value in Hz or GHz, or a string with units
    velocityLSRK: floating point value in km/s
    datestring:  "YYYY/MM/DD/HH:MM:SS" (format = image header keyword 'date-obs')
    ra: string "HH:MM:SS.SSSS"
    dec: string "DD.MM.SS.SSSS" or "DD:MM:SS.SSSS" (colons will be replaced with .)
    prec: only used to display the value when verbose=True
    Returns: the Rest frequency in Hz
    -Todd Hunter
    """
    if (dec.find(':') >= 0):
        dec = dec.replace(':','.')
        if verbose:
            print "Warning: replacing colons with decimals in the dec field."
    freqGHz = parseFrequencyArgumentToGHz(lsrkFrequency)
    myqa = createCasaTool(qatool)
    myme = createCasaTool(metool)
    velocityRadio = create_casa_quantity(myqa,velocityLSRK,"km/s")
    position = myme.direction(equinox, ra, dec)
    obstime = myme.epoch('TAI', datestring)
    dopp = myme.doppler("RADIO",velocityRadio)
    radialVelocityLSRK = myme.toradialvelocity("LSRK",dopp)
    myme.doframe(position)
    myme.doframe(myme.observatory(observatory))
    myme.doframe(obstime)
    rvelRad = myme.measure(radialVelocityLSRK,'LSRK')
    doppRad = myme.todoppler('RADIO', rvelRad)
    freqRad = myme.torestfrequency(me.frequency('LSRK',str(freqGHz)+'GHz'), dopp)
    myqa.done()
    myme.done()
    return freqRad['m0']['value']

def restToTopo(restFrequency, velocityLSRK, datestring, ra, dec, equinox='J2000', 
               observatory='ALMA', veltype='radio', verbose=False):
    """
    Converts a rest frequency, LSRK velocity, and observing date/direction
    to the corresponding frequency in the TOPO frame.
    Inputs:
    restFrequency: floating point value in Hz or GHz, or a string with units
    velocityLSRK: floating point value in km/s
    datestring:  "YYYY/MM/DD/HH:MM:SS"
    ra: string "HH:MM:SS.SSSS"
    dec: string "DD.MM.SS.SSSS" or "DD:MM:SS.SSSS" (colons will be replaced with .)
    prec: only used to display the value when verbose=True
    Returns: the TOPO frequency in Hz
    -Todd Hunter
    """
    topoFreqHz, diff1, diff2 = frames(velocityLSRK, datestring, ra, dec, equinox, 
                                      observatory, verbose=verbose,
                                      restFreq=restFrequency, veltype=veltype)
    return topoFreqHz

def frames(velocity=286.7, datestring="2005/11/01/00:00:00",
           ra="05:35:28.105", dec="-069.16.10.99", equinox="J2000", 
           observatory="ALMA", prec=4, verbose=True, myme='', myqa='',
           restFreq=345.79599, veltype='optical'):
    """
    Converts an optical velocity into barycentric, LSRK and TOPO frames.
    Converts a radio LSRK velocity into TOPO frame.
    Inputs:
    velocity: in km/s
    datestring:  "YYYY/MM/DD/HH:MM:SS"
    ra: "05:35:28.105"
    dec: "-069.16.10.99"
    equinox: "J2000" 
    observatory: "ALMA"
    prec: precision to display (digits to the right of the decimal point)
    veltype: 'radio' or 'optical'
    restFreq: in Hz, GHz or a string with units
    Returns: 
    * TOPO frequency in Hz
    * difference between LSRK-TOPO in km/sec
    * difference between LSRK-TOPO in Hz
    - Todd Hunter
    """
    localme = False
    localqa = False
    if (myme == ''):
        myme = createCasaTool(metool)
        localme = True
    if (myqa == ''):
        myqa = createCasaTool(qatool)
        localqa = True
    if (dec.find(':') >= 0):
        dec = dec.replace(':','.')
    position = myme.direction(equinox, ra, dec)
    obstime = myme.epoch('TAI', datestring)

    if (veltype.lower().find('opt') == 0):
        velOpt = create_casa_quantity(myqa,velocity,"km/s")
        dopp = myme.doppler("OPTICAL",velOpt)
        # CASA doesn't do Helio, but difference to Bary is hopefully small
        rvelOpt = myme.toradialvelocity("BARY",dopp)
    elif (veltype.lower().find('rad') == 0):
        rvelOpt = myme.radialvelocity('LSRK',str(velocity)+'km/s')
    else:
        print "veltype must be 'rad'io or 'opt'ical"
        return

    myme.doframe(position)
    myme.doframe(myme.observatory(observatory))
    myme.doframe(obstime)
    myme.showframe()

    rvelRad = myme.measure(rvelOpt,'LSRK')
    doppRad = myme.todoppler("RADIO",rvelRad)       
    restFreq = parseFrequencyArgumentToGHz(restFreq)
    freqRad = myme.tofrequency('LSRK',doppRad,me.frequency('rest',str(restFreq)+'GHz'))

    lsrk = qa.tos(rvelRad['m0'],prec=prec)
    rvelTop = myme.measure(rvelOpt,'TOPO')
    doppTop = myme.todoppler("RADIO",rvelTop)       
    freqTop = myme.tofrequency('TOPO',doppTop,me.frequency('rest',str(restFreq)+'GHz'))
    if (localme):
        myme.done()
    if (localqa):
        myqa.done()
    topo = qa.tos(rvelTop['m0'],prec=prec)
    velocityDifference = 0.001*(rvelRad['m0']['value']-rvelTop['m0']['value'])
    frequencyDifference = freqRad['m0']['value'] - freqTop['m0']['value']
    return(freqTop['m0']['value'], velocityDifference, frequencyDifference)

def parseFrequencyArgumentToGHz(bandwidth):
    """
    Converts a frequency string into floating point in GHz, based on the units.
    If the units are not present, then the value is assumed to be GHz if less
    than 1000.
    -Todd Hunter
    """
    value = parseFrequencyArgument(bandwidth)
    if (value > 1000):
        value *= 1e-9
    return(value)

def parseFrequencyArgument(bandwidth):
    """
    Converts a string frequency into floating point in Hz, based on the units.
    If the units are not present, then the value is simply converted to float.
    -Todd Hunter
    """
    bandwidth = str(bandwidth)
    ghz = bandwidth.lower().find('ghz')
    mhz = bandwidth.lower().find('mhz')
    khz = bandwidth.lower().find('khz')
    hz = bandwidth.lower().find('hz')
    if (ghz>0):
        bandwidth = 1e9*float(bandwidth[:ghz])
    elif (mhz>0):
        bandwidth = 1e6*float(bandwidth[:mhz])
    elif (khz>0):
        bandwidth = 1e3*float(bandwidth[:khz])
    elif (hz>0):
        bandwidth = float(bandwidth[:hz])
    else:
        bandwidth = float(bandwidth)
    return(bandwidth)

def channelSelectionRangesToIndexArray(selection, separator=';'):
    """
    Convert a channel selection range string to integer array of indices.
    e.g.:  '3~8;10~11' -> [3,4,5,6,7,8,10,11]
    """
    index = []
    for s in selection.split(separator):
        a,b = s.split('~')
        index += range(int(a),int(b)+1,1)
    return(np.array(index))

def linfit(x, y, yerror, pinit=[0,0]):
    """
    Basic linear function fitter with error bars in y-axis data points.
    Uses scipy.optimize.leastsq().  Accepts either lists or arrays.
    Example:
         lf = au.linfit()
         lf.linfit(x, y, yerror, pinit)
    Input:
         x, y: x and y axis values
         yerror: uncertainty in the y-axis values (vector or scalar)
         pinit contains the initial guess of [slope, intercept]
    Output:
       The fit result as: [slope, y-intercept]
    - Todd Hunter
    """
    x = np.array(x)
    y = np.array(y)
    if (type(yerror) != list and type(yerror) != np.ndarray):
        yerror = np.ones(len(x)) * yerror
    fitfunc = lambda p, x: p[1] + p[0]*x
    errfunc = lambda p,x,y,err: (y-fitfunc(p,x))/(err**2)
    out = scipy.optimize.leastsq(errfunc, pinit, args=(x,y,yerror/y), full_output=1)
    p = out[0]
    covar = out[1]
    return(p)

def polyfit(x, y, yerror, pinit=[0,0,0,0]):
    """
    Basic second-order polynomial function fitter with error bars in y-axis data points.
    Uses scipy.optimize.leastsq().  Accepts either lists or arrays.
    Input:
         x, y: x and y axis values
         yerror: uncertainty in the y-axis values (vector or scalar)
         pinit contains the initial guess of [slope, intercept]
         y = order2coeff*(x-xoffset)**2 + slope*(x-xoffset) + y-intercept
    Output:
       The fit result as: [xoffset, order2coeff, slope, y-intercept]
    - Todd Hunter
    """
    x = np.array(x)
    y = np.array(y)
    pinit[2] = np.mean(y)
    pinit[3] = x[len(x)/2]
    if (type(yerror) != list and type(yerror) != np.ndarray):
        yerror = np.ones(len(x)) * yerror
    fitfunc = lambda p, x: p[2] + p[1]*(x-p[3]) + p[0]*(x-p[3])**2
    errfunc = lambda p,x,y,err: (y-fitfunc(p,x))/(err**2)
    out = scipy.optimize.leastsq(errfunc, pinit, args=(x,y,yerror/y), full_output=1)
    p = out[0]
    covar = out[1]
    return(p)

def channelsInLargestGroup(selection):
    """
    Returns the number of channels in the largest group of channels in a
    CASA selection string.
    """
    if (selection == ''):
        return 0
    ranges = selection.split(';')
    largest = 0
    for r in ranges:
        c0 = int(r.split('~')[0])
        c1 = int(r.split('~')[1])
        channels = c1-c0+1
        if (channels > largest):
            largest = channels
    return largest

def countChannelsInRanges(channels, separator=';'):
    """
    Counts the number of channels in one spw of a CASA channel selection string
    and return a list of numbers.
    e.g. "5~20;30~40"  yields [16,11]
    """
    tokens = channels.split(separator)
    count = []
    for i in range(len(tokens)):
        c0 = int(tokens[i].split('~')[0])
        c1 = int(tokens[i].split('~')[1])
        count.append(c1-c0+1)
    return count

def countChannels(channels):
    """
    Counts the number of channels in a CASA channel selection string.
    If multiple spws are found, then it returns a dictionary of counts:
    e.g. "1:5~20;30~40"  yields 27; or  '6~30' yields 25
         "1:5~20;30~40,2:6~30" yields {1:27, 2:25}
    """
    if (channels == ''):
        return 0
    tokens = channels.split(',')
    nspw = len(tokens)
    count = {}
    for i in range(nspw):
        string = tokens[i].split(':')
        if (len(string) == 2):
            spw,string = string
        else:
            string = string[0]
            spw = 0
        ranges = string.split(';')
        for r in ranges:
            c0 = int(r.split('~')[0])
            c1 = int(r.split('~')[1])
            if (c0 > c1):
                casalogPost("Invalid channel range: c0 > c1 (%d > %d)" % (c0,c1))
                return
        channels = [1+int(r.split('~')[1])-int(r.split('~')[0]) for r in ranges]
        count[spw] = np.sum(channels)
    if (nspw == 1):
        count = count[spw]
    return(count)

def grep(filename, arg):
    """
    Runs grep for string <arg> in a subprocess and reports stdout and stderr.
    -Todd Hunter
    """
    process = subprocess.Popen(['grep', '-n', arg, filename], stdout=subprocess.PIPE)
    stdout, stderr = process.communicate()
    return stdout, stderr

    
def widthOfMaskArcsec(mask):
    """
    Finds width of the smallest central square that circumscribes all masked pixels.
    Returns the value in arcsec.
    """
    myia = createCasaTool(iatool)
    myia.open(mask)
    pixels = myia.getregion()
    myia.close()
    idx = np.where(pixels==True)
    leftRadius = np.shape(pixels)[0]/2 - np.min(idx[0])
    rightRadius = np.max(idx[0]) - np.shape(pixels)[0]/2
    width = 2*np.max(np.abs([leftRadius,rightRadius]))
    topRadius = np.max(idx[1]) - np.shape(pixels)[1]/2
    bottomRadius = np.shape(pixels)[1]/2 - np.min(idx[1]) 
    height = 2*np.max(np.abs([topRadius,bottomRadius]))
    cdelt1 = imhead(mask,mode='get',hdkey='cdelt1')
    if (cdelt1['unit'] == 'rad'):
        cdelt1 = np.degrees(cdelt1['value'])*3600.
    elif (cdelt1['unit'] == 'deg'):
        cdelt1 *= 3600.
    else:
        print "Unrecognized angular unit: ", cdelt1['unit']
        return
    width = np.abs(cdelt1)*(np.max([width,height])+1)
    return width

def checkForTriangularWavePattern(img, header, triangleFraction=0.83, pad=20):
    """
    Fit and remove linear slopes to each half of the spectrum, then comparse
    the MAD of the residual to the MAD of the original spectrum
    pad: fraction of total channels to ignore on each edge (e.g. 20: 1/20th)
    triangleFraction: MAD of dual-linfit residual must be less than this fraction*originalMAD
    Returns: 
    * Boolean: whether triangular pattern meets the threshold
    * value: either False (if slope test failed) or a float (the ratio of the MADs)
    """
    casalogPost('Checking for triangular wave pattern in the noise')
    chanlist, mad = computeMadSpectrum(img, header)
    nchan = len(chanlist)
#    n0 = nchan/2 #- nchan/pad
#    n1 = nchan/2 #+ nchan/pad
    n0 = nchan/2 - nchan/pad
    n1 = nchan/2 + nchan/pad
    slope = 0
    intercept = np.mean(mad[nchan/pad:-nchan/pad])
    slope0, intercept0 = linfit(chanlist[nchan/pad:n0], mad[nchan/pad:n0], MAD(mad[nchan/pad:n0]))
    slope1, intercept1 = linfit(chanlist[n1:-nchan/pad], mad[n1:-nchan/pad], MAD(mad[n1:-nchan/pad]))
    casalogPost("checkForTriangularWavePattern: slope0=%+g, slope1=%+g, %s" % (slope0,slope1,os.path.basename(img)))
    slopeTest = slope0 > 0 and slope1 < 0 
    slopeDiff = abs(abs(slope1)-abs(slope0))
    slopeSum = (abs(slope1)+abs(slope0))
    similarSlopeThreshold = 0.70 # 0.40
    similarSlopes = slopeDiff/slopeSum < similarSlopeThreshold 
    # if the slope is sufficiently high, then it is probably a real feature, not a noise feature
    slopeSignPattern = slope0 > 0 and slope1 < 0
    largeSlopes = abs(slope0)>1e-5 or abs(slope1)>1e-5
    differentSlopeThreshold = 5
    differentSlopes = (abs(slope0/slope1) > differentSlopeThreshold) or (abs(slope0/slope1) < 1.0/differentSlopeThreshold)
    slopeTest = (slopeSignPattern and similarSlopes and not largeSlopes) or (differentSlopes and not largeSlopes)
    if slopeTest:
        residual = mad - (chanlist*slope + intercept)
        madOfResidualSingleLine = MAD(residual)  
        residual0 = mad[:nchan/2] - (chanlist[:nchan/2]*slope0 + intercept0)
        residual1 = mad[nchan/2:] - (chanlist[nchan/2:]*slope1 + intercept1)
        madOfResidual = MAD(list(residual0) + list(residual1))
        madRatio = madOfResidual/madOfResidualSingleLine
#        fraction = 0.17
#        threshold = fraction*(np.max(mad[nchan/pad:-nchan/pad])-
#                              np.min(mad[nchan/pad:-nchan/pad]))
        casalogPost("checkForTriangularWavePattern: MAD_of_residual=%e, thresholdFraction=%.2f, ratio_of_MADs=%.3f, slopeDiff/slopeSum=%.2f, slope0/slope1=%.2f, signPattern=%s similarSlopes=%s largeSlopes=%s differentSlopes=%s" % (madOfResidual, triangleFraction, madRatio, slopeDiff/slopeSum,slope0/slope1,slopeSignPattern,similarSlopes,largeSlopes,differentSlopes))
        if (madRatio < triangleFraction):
            return True, madRatio
        else:
            return False, madRatio
    else:
        casalogPost("checkForTriangularWavePattern: slopeDiff/slopeSum=%.2f signPattern=%s similarSlopes=%s largeSlopes=%s  %s" % (slopeDiff/slopeSum,slopeSignPattern,similarSlopes,largeSlopes,os.path.basename(img)))
    return False, slopeTest
    
def computeMadSpectrum(img, header, box='', listit=False, verbose=False):
    """
    Uses the imstat task to compute an array containing the MAD spectrum of a cube.
    """
    axis = findSpectralAxis(img)
    if (header ==''):
        myshape = imheadlist(img,omitBeam=True)['shape']
    else:
        myshape = header['shape']
    axes = range(len(myshape))
    axes.remove(axis)
    nchan = myshape[axis]
    chanlist = np.array(range(nchan))
    casalogPost("Computing MAD spectrum.")
    mydict = imstat(img, axes=axes, box=box, listit=listit, verbose=verbose)
    return(chanlist, mydict['medabsdevmed'])

def imheadlist(vis, omitBeam=False):
    """
    Emulates imhead(mode='list') but leaves off the min/max/minpos/maxpos
    keywords, the filling of which makes it take so long to run on large cubes.
    -Todd Hunter
    """
    if (not os.path.exists(vis)):
        print "Could not find image."
        return
    header = {}
    keys = ['bunit','date-obs','equinox','imtype','masks',
            'object','observer','projection','reffreqtype',
            'restfreq','shape','telescope']
    if not omitBeam:
        singleBeam = imhead(vis, mode='get', hdkey='beammajor')
        if (singleBeam == False):
            header = imhead(vis, mode='list')
            if (header is None):
                print "No beam found.  Re-run with omitBeam=True."
                return -1
            if ('perplanebeams' not in header.keys()):
                print "No beam found.  Re-run with omitBeam=True."
                return -1
            beammajor = []
            beamminor = []
            beampa = []
            for beamchan in range(header['perplanebeams']['nChannels']):
                beamdict = header['perplanebeams']['*'+str(beamchan)]
                beammajor.append(beamdict['major']['value'])
                beamminor.append(beamdict['minor']['value'])
                beampa.append(beamdict['positionangle']['value'])
            bmaj = np.median(beammajor)
            bmin = np.median(beamminor)
            sinbpa = np.sin(np.radians(np.array(beampa)))
            cosbpa = np.cos(np.radians(np.array(beampa)))
            bpa = degrees(np.median(np.arctan2(np.median(sinbpa), np.median(cosbpa))))
            header['beammajor'] = bmaj
            header['beamminor'] = bmin
            header['beampa'] = bpa
        else:
            keys += ['beammajor','beamminor','beampa']
    for key in keys:
        try:
            header[key] = imhead(vis, mode='get', hdkey=key)
        except:
            pass
    for axis in range(len(header['shape'])):
        for key in ['cdelt','crval']:
            mykey = key+str(axis+1)
            try:
                result = imhead(vis, mode='get', hdkey=mykey)
                if (type(result) == dict):
                    header[mykey] = result['value']
                else:
                    # crval3 (pol axis) will be an array (e.g. ['I']) not a dict
                    header[mykey] = result
            except:
                print "Failed to set header key: ", mykey
                pass
        for key in ['crpix','ctype','cunit']:
            mykey = key+str(axis+1)
            try:
                header[mykey] = imhead(vis, mode='get', hdkey=mykey)
            except:
                pass
    return(header)

def isSingleContinuum(vis, spw='', source='', intent='OBSERVE_TARGET', 
                      verbose=False, mymsmd=None):
    """
    Checks whether an spw was defined as single continuum in the OT by
    looking at the transition name in the SOURCE table of a measurement set.
    vis: a single measurement set, a comma-delimited list, or a python list 
         (only the first one will be used)
    Optional parameters:
    spw: can be integer ID or string integer ID; if not specified, then it 
         will use the first science spw
    source: passed to transition(); if not specified, it will use the first one
    intent: if source is blank then use first one with matching intent and spw
    mymsmd: an existing instance of the msmd tool
    -Todd Hunter
    """
    if vis=='': return False
    if type(vis) == list or type(vis) == np.ndarray:
        vis = vis[0]
    else:
        vis = vis.split(',')[0]
    if not os.path.exists(vis): return False
    needToClose = False
    if spw=='':
        if mymsmd is None:
            needToClose = True
            mymsmd = createCasaTool(msmdtool)
            mymsmd.open(vis)
        spw = getScienceSpws(vis, returnString=False, mymsmd=mymsmd)[0]
    info = transition(vis, spw, source, intent, verbose, mymsmd)
    if needToClose:
        mymsmd.close()
    if len(info) > 0:
        if info[0].find('Single_Continuum') >= 0:
            casalogPost("Identified spectral setup as Single_Continuum from transition name.")
            return True
    return False
    
def transition(vis, spw, source='', intent='OBSERVE_TARGET', 
               verbose=True, mymsmd=None):
    """
    Returns the list of transitions for specified spw (and source).
    vis: measurement set
    spw: can be integer ID or string integer ID
    Optional parameters:
    source: can be integer ID or string name; if not specified, use the first one
    intent: if source is blank then use first one with matching intent and spw
    """
    if (not os.path.exists(vis)):
        print "Could not find measurement set"
        return
    needToClose = False
    if mymsmd is None:
        needToClose = True
        mymsmd = createCasaTool(msmdtool)
        mymsmd.open(vis)
    spw = int(spw)
    if (spw >= mymsmd.nspw()):
        print "spw not in the dataset"
        if needToClose:
            mymsmd.close()
        return
    mytb = createCasaTool(tbtool)
    mytb.open(vis+'/SOURCE')
    spws = mytb.getcol('SPECTRAL_WINDOW_ID')
    sourceIDs = mytb.getcol('SOURCE_ID')
    names = mytb.getcol('NAME')
    spw = int(spw)
    if (type(source) == str):
        if (source.isdigit()):
            source = int(source)
        elif (source == ''):
            # pick source
            fields1 = mymsmd.fieldsforintent(intent+'*')
            fields2 = mymsmd.fieldsforspw(spw)
            fields = np.intersect1d(fields1,fields2)
            source = mymsmd.namesforfields(fields[0])[0]
            if verbose:
                print "For spw %d, picked source: " % (spw), source
    if (type(source) == str or type(source) == np.string_):
        sourcerows = np.where(names==source)[0]
        if (len(sourcerows) == 0):
            # look for characters ()/ and replace with underscore
            names = np.array(sanitizeNames(names))
            sourcerows = np.where(source==names)[0]
    else:
        sourcerows = np.where(sourceIDs==source)[0]
        
    spwrows = np.where(spws==spw)[0]
    row = np.intersect1d(spwrows, sourcerows)
    if (len(row) > 0):
        if (mytb.iscelldefined('TRANSITION',row[0])):
            transitions = mytb.getcell('TRANSITION',row[0])
        else:
            transitions = []
    else:
        transitions = []
    if (len(transitions) == 0):
        print "No value found for this source/spw (row=%s)." % row
    mytb.close()
    if needToClose:
        mymsmd.close()
    return(transitions)

def getScienceSpws(vis, intent='OBSERVE_TARGET#ON_SOURCE', returnString=True, 
                   tdm=True, fdm=True, mymsmd=None, sqld=False):
    """
    Return a list of the each spw with the specified intent.  For ALMA data,
    it ignores channel-averaged and SQLD spws.
    returnString: if True, it returns: '1,2,3'
                  if False, it returns: [1,2,3]
    """
    needToClose = False
    if (mymsmd is None or mymsmd == ''):
        mymsmd = createCasaTool(msmdtool)
        mymsmd.open(vis)
        needToClose = True
    if (intent not in mymsmd.intents()):
        if intent.split('#')[0] in [i.split('#')[0] for i in mymsmd.intents()]:
            # VLA uses OBSERVE_TARGET#UNSPECIFIED, so try that before giving up
            intent = intent.split('#')[0]+'*'
        else:
            casalogPost("Intent %s not in dataset (nor is %s*)." % (intent,intent.split('#')[0]))
    spws = mymsmd.spwsforintent(intent)
    observatory = getObservatoryName(vis)
    if (observatory.find('ALMA') >= 0 or observatory.find('OSF') >= 0):
        almaspws = mymsmd.almaspws(tdm=tdm,fdm=fdm,sqld=sqld)
        if (len(spws) == 0 or len(almaspws) == 0):
            scienceSpws = []
        else:
            scienceSpws = np.intersect1d(spws,almaspws)
    else:
        scienceSpws = spws
    if needToClose:
        mymsmd.close()
    if (returnString):
        return(','.join(str(i) for i in scienceSpws))
    else:
        return(list(scienceSpws))

def getObservatoryName(vis):
    """
    Returns the observatory name in the specified ms.
    """
    antTable = vis+'/OBSERVATION'
    try:
        mytb = createCasaTool(tbtool)
        mytb.open(antTable)
        myName = mytb.getcell('TELESCOPE_NAME')
        mytb.close()
    except:
        casalogPost("Could not open OBSERVATION table to get the telescope name: %s" % (antTable))
        myName = ''
    return(myName)

def makeUvcontsub(files='*.dat', fitorder=1, useFrequency=False):
    """
    Takes a list of output files from findContinuum and builds an spw selection
    for uvcontsub, then prints the resulting commands to the screen.
    files: a list of files, either a comma-delimited string, a python list, or a wildcard string
    fitorder: passed through to the uvcontsub
    useFrequency: if True, then produce selection string in frequency; otherwise use channels
      Note: frequencies in the findContinuum .dat file are topo, which is what uvcontsub wants.
    """
    if files.find('*') >= 0:
        resultFiles = sorted(glob.glob(files))
        uids = []
        for resultFile in resultFiles:
            uid = resultFile.split('.')[0]
        if len(np.unique(uids)) > 1:
            print "There are results for more than one OUS in this directory.  Be more specific."
            return
    elif type(files) == str:
        resultFiles = sorted(files.split(','))
    else:
        resultFiles = sorted(files)
    freqRanges = {}
    spws = []
    for rf in resultFiles:
        spw = rf.split('spw')[1].split('.')[0]
        spws.append(spw)
        uid = rf.split('.')[0]
        field = rf[len(uid)+6:].split('_')[1]
        f = open(rf,'r')
        lines = f.readlines()
        f.close()
        for line in lines:
            tokens = line.split()
            if line == lines[0]:
                channelRanges = tokens[0]
            if len(tokens) == 2:
                uid = tokens[0]
                if uid not in freqRanges.keys():
                    freqRanges[uid] = {}
                if field not in freqRanges[uid].keys():
                    freqRanges[uid][field] = ''
                else:
                    freqRanges[uid][field] += ','
                if useFrequency:
                    freqRanges[uid][field] += '%s:%s' % (spw,tokens[1])
                else:
                    freqRanges[uid][field] += '%s:%s' % (spw,channelRanges)
    spws = ','.join(np.unique(spws))
    if freqRanges == {} and useFrequency:
        print "There are no frequency ranges in the *.dat files.  You need to run findContinuum with the 'vis' parameter specified."
        return
    for uid in freqRanges.keys():
        for field in freqRanges[uid].keys():
            print "uvcontsub('%s', field='%s', fitorder=%d, spw='%s', fitspw='%s')\n" % (uid, field, fitorder, spws, freqRanges[uid][field])

