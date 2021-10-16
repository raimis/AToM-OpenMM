from __future__ import print_function
from __future__ import division
"""
Collects all of the ways that openmm systems are loaded
"""
import os, re, sys, time, shutil, copy, random, signal
import multiprocessing as mp
#from multiprocessing import Process, Queue, Event
import logging

from simtk import openmm as mm
from simtk.openmm.app import *
from simtk.openmm import *
from simtk.unit import *
from datetime import datetime
from configobj import ConfigObj

from atmmetaforce import *

class OMMSystem(object):
    def __init__(self, basename, keywords):
        self.system = None
        self.topology = None
        self.positions = None
        self.boxvectors = None
        self.integrator = None
        self.keywords = keywords
        self.basename = basename

        #parameters stored in the openmm state
        self.parameter = {}
        self.parameter['stateid'] = 'REStateId'
        self.parameter['cycle'] = 'RECycle'
        self.parameter['mdsteps'] = 'REMDSteps'
        #more ATM property names are in atmmetaforce

        #parameters from the cntl file
        self.cparams = {}
        
        self.frictionCoeff = float(self.keywords.get('FRICTION_COEFF')) / picosecond
        self.MDstepsize = float(self.keywords.get('TIME_STEP')) * picosecond
        
    def _exit(message):
        """Print and flush a message to stdout and then exit."""
        print(message)
        sys.stdout.flush()
        sys.exit(1)

#Temperature RE
class OMMSystemAmberTRE(OMMSystem):
    def __init__(self, basename, keywords, prmtopfile, crdfile):
        super().__init__(basename, keywords)
        self.prmtopfile = prmtopfile
        self.crdfile = crdfile
        self.parameter['temperature'] = 'RETemperature'
        self.parameter['potential_energy'] = 'REPotEnergy'
        
    def create_system(self):

        self.prmtop = AmberPrmtopFile(self.prmtopfile)
        self.inpcrd = AmberInpcrdFile(self.crdfile)
        self.system = self.prmtop.createSystem(nonbondedMethod=PME, nonbondedCutoff=1*nanometer,
                                          constraints=HBonds)
        self.topology = self.prmtop.topology
        self.positions = self.inpcrd.positions

        #the temperature defines the state and will be overriden in set_state()
        temperature = 300 * kelvin

        #add barostat
        barostat = MonteCarloBarostat(1*bar, temperature)
        barostat.setForceGroup(1)
        barostat.setFrequency(0)#disabled
        self.system.addForce(barostat)

        #hack to store ASyncRE quantities in the openmm State
        sforce = mm.CustomBondForce("1")
        for name in self.parameter:
            sforce.addGlobalParameter(self.parameter[name], 0)
        sforce.setForceGroup(1)
        self.system.addForce(sforce)
        
        self.integrator = LangevinIntegrator(temperature/kelvin, self.frictionCoeff/(1/picosecond), self.MDstepsize/ picosecond )

class OMMSystemAmberABFE(OMMSystem):
    def __init__(self, basename, keywords, prmtopfile, crdfile):
        super().__init__(basename, keywords)
        self.prmtopfile = prmtopfile
        self.crdfile = crdfile
        self.parameter['temperature'] = 'RETemperature'
        self.parameter['potential_energy'] = 'REPotEnergy'
        self.parameter['perturbation_energy'] = 'REPertEnergy'
        self.atmforce = None
        
    def create_system(self):

        self.prmtop = AmberPrmtopFile(self.prmtopfile)
        self.inpcrd = AmberInpcrdFile(self.crdfile)
        self.system = self.prmtop.createSystem(nonbondedMethod=PME, nonbondedCutoff=1*nanometer,
                                          constraints=HBonds)
        self.topology = self.prmtop.topology
        self.positions = self.inpcrd.positions
        self.boxvectors = self.inpcrd.boxVectors
        
        atm_utils = ATMMetaForceUtils(self.system)
        
        lig_atoms = self.keywords.get('LIGAND_ATOMS')   #indexes of ligand atoms
        if lig_atoms:
            lig_atoms = [int(i) for i in lig_atoms]
        else:
            msg = "Error: LIGAND_ATOMS is required"
            self._exit(msg)
        
        cm_lig_atoms = self.keywords.get('LIGAND_CM_ATOMS')   #indexes of ligand atoms for CM-CM Vsite restraint
        if cm_lig_atoms:
            lig_atom_restr = [int(i) for i in cm_lig_atoms]
        else:
            lig_atom_restr = None

        cm_rcpt_atoms = self.keywords.get('RCPT_CM_ATOMS')   #indexes of rcpt atoms for CM-CM Vsite restraint
        if cm_rcpt_atoms:
            rcpt_atom_restr = [int(i) for i in cm_rcpt_atoms]
        else:
            rcpt_atom_restr = None

        cmrestraints_present = (cm_rcpt_atoms is not None) and (cm_lig_atoms is not None)
        
        if cmrestraints_present:
            cmkf = float(self.keywords.get('CM_KF'))
            kf = cmkf * kilocalorie_per_mole/angstrom**2 #force constant for Vsite CM-CM restraint
            cmtol = float(self.keywords.get('CM_TOL'))
            r0 = cmtol * angstrom #radius of Vsite sphere
            ligoffset = self.keywords.get('LIGOFFSET')
            if ligoffset:
                ligoffset = [float(offset) for offset in ligoffset.split(',')]*angstrom
            atm_utils.addRestraintForce(lig_cm_particles = lig_atom_restr,
                                        rcpt_cm_particles = rcpt_atom_restr,
                                        kfcm = kf,
                                        tolcm = r0,
                                        offset = ligoffset)

        #indexes of the atoms whose position is restrained near the initial positions
        #by a flat-bottom harmonic potential. 
        posrestr_atoms_list = self.keywords.get('POS_RESTRAINED_ATOMS')
        if posrestr_atoms_list:
            posrestr_atoms = [int(i) for i in posrestr_atoms_list]
            fc = float(self.keywords.get('POSRE_FORCE_CONSTANT')) * kilocalorie_per_mole
            tol = float(self.keywords.get('POSRE_TOLERANCE')) * angstrom
            atm_utils.addPosRestraints(posrestr_atoms, self.positions, fc, tol)
            
        #these define the state and will be overriden in set_state()
        temperature = 300 * kelvin
        lmbd = 0.0
        lambda1 = lmbd
        lambda2 = lmbd
        alpha = 0.0 / kilocalorie_per_mole
        u0 = 0.0 * kilocalorie_per_mole
        w0coeff = 0.0 * kilocalorie_per_mole

        #soft-core parameters are fixed (the same in all states)
        umsc = float(self.keywords.get('UMAX')) * kilocalorie_per_mole
        ubcore = self.keywords.get('UBCORE')
        if ubcore:
            ubcore = float(ubcore) * kilocalorie_per_mole
        else:
            ubcore = 0.0 * kilocalorie_per_mole
        acore = float(self.keywords.get('ACORE'))

        if not (self.keywords.get('DISPLACEMENT') is None):
            self.displ = [float(displ) for displ in self.keywords.get('DISPLACEMENT').split(',')]*angstrom
        else:
            msg = "Error: DISPLACEMENT is required"
            self._exit(msg)
        
        #create ATM Force
        self.atmforce = ATMMetaForce(lambda1, lambda2,  alpha * kilojoules_per_mole, u0/kilojoules_per_mole, w0coeff/kilojoules_per_mole, umsc/kilojoules_per_mole, ubcore/kilojoules_per_mole, acore)

        for i in range(self.topology.getNumAtoms()):
            self.atmforce.addParticle(i, 0., 0., 0.)
        for i in lig_atoms:
            self.atmforce.setParticleParameters(i, i, self.displ[0], self.displ[1], self.displ[2] )
        self.atmforce.setForceGroup(3)
        self.system.addForce(self.atmforce)
        
        #add barostat
        barostat = MonteCarloBarostat(1*bar, temperature)
        barostat.setForceGroup(1)                                                         
        barostat.setFrequency(0)#disabled
        self.system.addForce(barostat)

        #hack to store ASyncRE quantities in the openmm State
        sforce = mm.CustomBondForce("1")
        for name in self.parameter:
            sforce.addGlobalParameter(self.parameter[name], 0)
        sforce.setForceGroup(1)
        self.system.addForce(sforce)
        
        #temperature = int(self.keywords.get('TEMPERATURES')) * kelvin
        self.integrator = LangevinIntegrator(temperature/kelvin, self.frictionCoeff/(1/picosecond), self.MDstepsize/ picosecond )
        self.integrator.setIntegrationForceGroups({1,3})

        #these are the global parameters specified in the cntl files that need to be reset after reading the first
        #configuration
        self.cparams["ATMUmax"] = umsc/kilojoules_per_mole
        self.cparams["ATMUbcore"] = ubcore/kilojoules_per_mole
        self.cparams["ATMAcore"] = acore

        
class OMMSystemAmberRBFE(OMMSystem):
    def __init__(self, basename, keywords, prmtopfile, crdfile):
        super().__init__(basename, keywords)
        self.prmtopfile = prmtopfile
        self.crdfile = crdfile
        self.parameter['temperature'] = 'RETemperature'
        self.parameter['potential_energy'] = 'REPotEnergy'
        self.parameter['perturbation_energy'] = 'REPertEnergy'
        
    def create_system(self):

        self.prmtop = AmberPrmtopFile(self.prmtopfile)
        self.inpcrd = AmberInpcrdFile(self.crdfile)
        self.system = self.prmtop.createSystem(nonbondedMethod=PME, nonbondedCutoff=1*nanometer,
                                          constraints=HBonds)
        self.topology = self.prmtop.topology
        self.positions = self.inpcrd.positions

        atm_utils = ATMMetaForceUtils(self.system)

        lig1_atoms = self.keywords.get('LIGAND1_ATOMS')   #indexes of ligand1 atoms
        lig2_atoms = self.keywords.get('LIGAND2_ATOMS')   #indexes of ligand2 atoms        
        if lig1_atoms:
            lig1_atoms = [int(i) for i in lig1_atoms]
        else:
            msg = "Error: LIGAND1_ATOMS is required"
            self._exit(msg)        
        if lig2_atoms:
            lig2_atoms = [int(i) for i in lig2_atoms]
        else:
            msg = "Error: LIGAND2_ATOMS is required"
            self._exit(msg)
        
        #ligand 1 Vsite restraint
        cm_lig1_atoms = self.keywords.get('REST_LIGAND1_CMLIG_ATOMS')   #indexes of ligand atoms for CM-CM Vsite restraint
        if cm_lig1_atoms:
            lig1_atom_restr = [int(i) for i in cm_lig1_atoms]
        else:
            lig1_atom_restr = None
        
        #ligand 2 Vsite restraint
        cm_lig2_atoms = self.keywords.get('REST_LIGAND2_CMLIG_ATOMS')   #indexes of ligand atoms for CM-CM Vsite restraint
        if cm_lig2_atoms:
            lig2_atom_restr = [int(i) for i in cm_lig2_atoms]
        else:
            lig2_atom_restr = None
        
        #Vsite restraint receptor atoms
        cm_rcpt_atoms = self.keywords.get('REST_LIGAND_CMREC_ATOMS')   #indexes of rcpt atoms for CM-CM Vsite restraint
        if cm_rcpt_atoms:
            rcpt_atom_restr = [int(i) for i in cm_rcpt_atoms]
        else:
            rcpt_atom_restr = None

        #set displacements and offsets for ligand 1 and ligand 2
        if self.keywords.get('DISPLACEMENT'):
            self.displ = [float(displ) for displ in self.keywords.get('DISPLACEMENT').split(',')]*angstrom
            self.lig1offset = [float(0.0*offset) for offset in self.displ/angstrom]*angstrom
            self.lig2offset = [float(offset) for offset in self.displ/angstrom]*angstrom
        else:
            msg = "DISPLACEMENT is required"
            self._exit(msg)
            
        cmrestraints_present = (rcpt_atom_restr is not None) and (lig1_atom_restr is not None) and (lig2_atom_restr is not None)

        if cmrestraints_present:
            cmkf = float(self.keywords.get('CM_KF'))
            kf = cmkf * kilocalorie_per_mole/angstrom**2 #force constant for Vsite CM-CM restraint
            cmtol = float(self.keywords.get('CM_TOL'))
            r0 = cmtol * angstrom #radius of Vsite sphere            
            
            #Vsite restraints for ligands 1 and 2
            atm_utils.addRestraintForce(lig_cm_particles = lig1_atom_restr,
                                        rcpt_cm_particles = rcpt_atom_restr,
                                        kfcm = kf,
                                        tolcm = r0,
                                        offset = self.lig1offset)
            atm_utils.addRestraintForce(lig_cm_particles = lig2_atom_restr,
                                        rcpt_cm_particles = rcpt_atom_restr,
                                        kfcm = kf,
                                        tolcm = r0,
                                        offset = self.lig2offset)
        
        #reference atoms for alignment force
        refatoms1_cntl = self.keywords.get('ALIGN_LIGAND1_REF_ATOMS')
        self.refatoms1 = [int(refatoms1) for refatoms1 in refatoms1_cntl]
        lig1_ref_atoms  = [ self.refatoms1[i]+lig1_atoms[0] for i in range(3)]
        
        refatoms2_cntl = self.keywords.get('ALIGN_LIGAND2_REF_ATOMS')
        self.refatoms2 = [int(refatoms2) for refatoms2 in refatoms2_cntl]
        lig2_ref_atoms  = [ self.refatoms2[i]+lig2_atoms[0] for i in range(3)]
        
        #add alignment force
        atm_utils.addAlignmentForce(liga_ref_particles = lig1_ref_atoms,
                                    ligb_ref_particles = lig2_ref_atoms,
                                    kfdispl = float(self.keywords.get('ALIGN_KF_SEP'))*kilocalorie_per_mole/angstrom**2,
                                    ktheta = float(self.keywords.get('ALIGN_K_THETA'))*kilocalorie_per_mole,
                                    kpsi = float(self.keywords.get('ALIGN_K_PSI'))*kilocalorie_per_mole,
                                    offset = self.lig2offset)

        #indexes of the atoms whose position is restrained near the initial positions
        #by a flat-bottom harmonic potential. 
        posrestr_atoms_list = self.keywords.get('POS_RESTRAINED_ATOMS')
        if posrestr_atoms_list:
            posrestr_atoms = [int(i) for i in posrestr_atoms_list]
            fc = float(self.keywords.get('POSRE_FORCE_CONSTANT')) * kilocalorie_per_mole
            tol = float(self.keywords.get('POSRE_TOLERANCE')) * angstrom
            atm_utils.addPosRestraints(posrestr_atoms, self.positions, fc, tol)
            
        #these define the state and will be overriden in set_state()
        temperature = 300 * kelvin
        lmbd = 0.0
        lambda1 = lmbd
        lambda2 = lmbd
        alpha = 0.0 / kilocalorie_per_mole
        u0 = 0.0 * kilocalorie_per_mole
        w0coeff = 0.0 * kilocalorie_per_mole

        #soft-core parameters are fixed (the same in all states)
        umsc = float(self.keywords.get('UMAX')) * kilocalorie_per_mole
        ubcore = self.keywords.get('UBCORE')
        if ubcore:
            ubcore = float(ubcore) * kilocalorie_per_mole
        else:
            ubcore = 0.0 * kilocalorie_per_mole
        acore = float(self.keywords.get('ACORE'))

        #create ATM Force
        self.atmforce = ATMMetaForce(lambda1, lambda2,  alpha * kilojoules_per_mole, u0/kilojoules_per_mole, w0coeff/kilojoules_per_mole, umsc/kilojoules_per_mole, ubcore/kilojoules_per_mole, acore)

        for i in range(self.topology.getNumAtoms()):
            self.atmforce.addParticle(i, 0., 0., 0.)
        for i in lig1_atoms:
            self.atmforce.setParticleParameters(i, i, self.displ[0], self.displ[1], self.displ[2] )
        for i in lig2_atoms:
            self.atmforce.setParticleParameters(i, i, -self.displ[0], -self.displ[1], -self.displ[2] )

        self.atmforce.setForceGroup(3)
        self.system.addForce(self.atmforce)

        #add barostat
        barostat = MonteCarloBarostat(1*bar, temperature)
        barostat.setForceGroup(1)
        barostat.setFrequency(0)#disabled
        self.system.addForce(barostat)

        #hack to store ASyncRE quantities in the openmm State
        sforce = mm.CustomBondForce("1")
        for name in self.parameter:
            sforce.addGlobalParameter(self.parameter[name], 0)
        sforce.setForceGroup(1)
        self.system.addForce(sforce)
        
        #temperature = int(self.keywords.get('TEMPERATURES')) * kelvin
        self.integrator = LangevinIntegrator(temperature/kelvin, self.frictionCoeff/(1/picosecond), self.MDstepsize/ picosecond )
        self.integrator.setIntegrationForceGroups({1,3})

        #these are the global parameters specified in the cntl files that need to be reset after reading the first
        #configuration
        self.cparams["ATMUmax"] = umsc/kilojoules_per_mole
        self.cparams["ATMUbcore"] = ubcore/kilojoules_per_mole
        self.cparams["ATMAcore"] = acore
