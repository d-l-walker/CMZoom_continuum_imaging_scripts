#!/bin/bash

: '
IMPORTANT: Before running this script on RTDC machines, you must first source IDL & Miriad via:

  source /reduction/czdata/final/final_images/Imaging_script/idl_setup
  source /reduction/czdata/final/final_images/Imaging_script/mir_setup
  source /home/miriad/miriad_WB/automiriad.csh


This script should be executed on the command line along with the name of the region and corresponding tracks to be imaged. E.g.:

  ./imscript.sh G0.380+0.050 /reduction/czdata/final/final_cal/track2_final_DW.mir /reduction/czdata/final/final_cal/track21_final_if1_xl.mir /reduction/czdata/final/final_cal/track21_final_if2_xl.mir

The script does the following:
  - Creates directory for given sourcename
  - Runs an IDL script (mir_output_to_miriad.pro) to output sideband data in .miriad format
  - For each .miriad file, edge channels are flagged and data are output as .UVFITS
  - Sourcename is converted into J2000 format to be used later in CASA
  - Run_find_cont.py is executed in CASA to determine continuum channels for each file
  - Run_tclean.py is executed in CASA to generate continuum mosaic from all data associated with the given region
'

files=("$@")

mkdir -m 777 ${files[0]}_MIR

/opt/rsi/idl_6.2/bin/idl -e ".r ./mir_output_to_miriad.pro" -args ${files[@]}


i=1
for element in ${@:2}
do
    if [[ -n $(find ./${files[0]}_MIR -maxdepth 1 -name "*_${asic}${i}.*") ]]
    then

        lsb_asic=${files[0]}.lsb_asic_$i
        usb_asic=${files[0]}.usb_asic_$i

        uvflag vis=${files[0]}_MIR/$lsb_asic.miriad edge=10,10,0 flagval=flag
        uvflag vis=${files[0]}_MIR/$usb_asic.miriad edge=10,10,0 flagval=flag

        fits in=${files[0]}_MIR/$lsb_asic.miriad op=uvout out=${files[0]}_MIR/$lsb_asic.fits
        fits in=${files[0]}_MIR/$usb_asic.miriad op=uvout out=${files[0]}_MIR/$usb_asic.fits

    fi

    if [[ -n $(find ./${files[0]}_MIR -maxdepth 1 -name "*_${swarm}${i}.*") ]]
    then

        lsb_swarm=${files[0]}.lsb_swarm_$i
        usb_swarm=${files[0]}.usb_swarm_$i

        uvflag vis=${files[0]}_MIR/$lsb_swarm.miriad edge=100,100,0 flagval=flag
        uvflag vis=${files[0]}_MIR/$usb_swarm.miriad edge=100,100,0 flagval=flag

        fits in=${files[0]}_MIR/$lsb_swarm.miriad op=uvout out=${files[0]}_MIR/$lsb_swarm.fits
        fits in=${files[0]}_MIR/$usb_swarm.miriad op=uvout out=${files[0]}_MIR/$usb_swarm.fits

    fi
        i=$(($i+1))
done

sourcename=${files[0]}
/reduction/czdata/final/final_images/Imaging_script/anaconda2/bin/python2.7 ./phasecenter.py $sourcename
j2000=$(<j2000.txt)
rm j2000.txt

/opt/casa-release-5.3.0-143.el6/bin/casa --nologger --nologfile -c ./Run_find_cont.py $j2000 ${files[@]}

/opt/casa-release-5.3.0-143.el6/bin/casa --nologger --nologfile -c ./Run_tclean.py $j2000 ${files[@]}
